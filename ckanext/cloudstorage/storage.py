#!/usr/bin/env python
# -*- coding: utf-8 -*-
import cgi
import mimetypes
import os.path
from six.moves.urllib.parse import urlparse
from ast import literal_eval
from datetime import datetime, timedelta
from time import time
from tempfile import SpooledTemporaryFile

from ckan.plugins.toolkit import config
from ckan import model
from ckan.lib import munge
from ckan.plugins.toolkit import get_action
import ckan.plugins as p

from libcloud.storage.types import Provider, ObjectDoesNotExistError
from libcloud.storage.providers import get_driver


from werkzeug.datastructures import FileStorage as FlaskFileStorage
ALLOWED_UPLOAD_TYPES = (cgi.FieldStorage, FlaskFileStorage)

import logging

log = logging.getLogger(__name__)

def _get_underlying_file(wrapper):
    if isinstance(wrapper, FlaskFileStorage):
        return wrapper.stream
    return wrapper.file


class CloudStorage(object):
    def __init__(self):
        self._driver_options = literal_eval(config['ckanext.cloudstorage.driver_options'])
        if 'S3' in self.driver_name and not self.driver_options:
            if self.aws_use_boto3_sessions:
                self.authenticate_with_aws_boto3()
            else:
                self.authenticate_with_aws()

        self.driver = get_driver(
            getattr(
                Provider,
                self.driver_name
            )
        )(**self.driver_options)
        self._container = None

    def path_from_filename(self, rid, filename):
        raise NotImplementedError

    def authenticate_with_aws(self):
        import requests
        r = requests.get("http://169.254.169.254/latest/meta-data/iam/security-credentials/")
        role = r.text
        r = requests.get("http://169.254.169.254/latest/meta-data/iam/security-credentials/" + role)
        credentials = r.json()
        self.driver_options = {'key': credentials['AccessKeyId'],
                               'secret': credentials['SecretAccessKey'],
                               'token': credentials['Token'],
                               'expires': credentials['Expiration']}

        self.driver = get_driver(
            getattr(
                Provider,
                self.driver_name
            )
        )(**self.driver_options)
        self._container = None

    def authenticate_with_aws_boto3(self):
        """
        TTL max 900 seconds for IAM role session
        https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use.html#id_roles_use_view-role-max-session
        """
        import boto3
        session = boto3.Session()
        credentials = session.get_credentials()
        current_credentials = credentials.get_frozen_credentials()
        self.driver_options = {'key': current_credentials.access_key,
                               'secret': current_credentials.secret_key,
                               'token': current_credentials.token,
                               'expires': datetime.fromtimestamp(time() + 900).strftime('%Y-%m-%dT%H:%M:%SZ')}

        self.driver = get_driver(
            getattr(
                Provider,
                self.driver_name
            )
        )(**self.driver_options)
        self._container = None

    @property
    def container(self):
        """
        Return the currently configured libcloud container.
        """
        if self.driver_options.get('expires'):
            expires = datetime.strptime(self.driver_options['expires'], "%Y-%m-%dT%H:%M:%SZ")
            if expires < datetime.utcnow():
                if self.aws_use_boto3_sessions:
                    self.authenticate_with_aws_boto3()
                else:
                    self.authenticate_with_aws()

        if self._container is None:
            self._container = self.driver.get_container(
                container_name=self.container_name
            )

        return self._container

    @property
    def driver_options(self):
        """
        A dictionary of options ckanext-cloudstorage has been configured to
        pass to the apache-libcloud driver.
        """
        return self._driver_options

    @driver_options.setter
    def driver_options(self, value):
        self._driver_options = value

    @property
    def driver_name(self):
        """
        The name of the driver (ex: AZURE_BLOBS, S3) that ckanext-cloudstorage
        is configured to use.


        .. note::

            This value is used to lookup the apache-libcloud driver to use
            based on the Provider enum.
        """
        return config['ckanext.cloudstorage.driver']

    @property
    def container_name(self):
        """
        The name of the container (also called buckets on some providers)
        ckanext-cloudstorage is configured to use.
        """
        return config['ckanext.cloudstorage.container_name']

    @property
    def use_secure_urls(self):
        """
        `True` if ckanext-cloudstroage is configured to generate secure
        one-time URLs to resources, `False` otherwise.
        """
        return p.toolkit.asbool(
            config.get('ckanext.cloudstorage.use_secure_urls', False)
        )


    @property
    def connection_link(self):
        """
        The connection link to the container
        """
        return "AccountName={};AccountKey={}".format(
            self.driver_options['key'],
            self.driver_options['secret']
        )


    @property
    def aws_use_boto3_sessions(self):
        """
        'True' if ckanext-cloudstorage is configured to use boto3 instead of
        boto for AWS IAM sessions. This makes session creation possible on
        platforms like AWS ECS Fargate or AWS Lambda.
        """
        return bool(int(config.get('ckanext.cloudstorage.aws_use_boto3_sessions', 0)))

    @property
    def leave_files(self):
        """
        `True` if ckanext-cloudstorage is configured to leave files on the
        provider instead of removing them when a resource/package is deleted,
        otherwise `False`.
        """
        return p.toolkit.asbool(
            config.get('ckanext.cloudstorage.leave_files', False)
        )

    @property
    def can_use_advanced_azure(self):
        """
        `True` if the `azure-storage` module is installed and
        ckanext-cloudstorage has been configured to use Azure, otherwise
        `False`.
        """
        # Are we even using Azure?
        if self.driver_name == 'AZURE_BLOBS':
            try:
                # Yes? Is the azure-storage package available?
                from azure import storage
                # Shut the linter up.
                assert storage
                return True
            except ImportError:
                pass

        return False

    @property
    def can_use_advanced_aws(self):
        """
        `True` if the `boto` module is installed and ckanext-cloudstorage has
        been configured to use Amazon S3, otherwise `False`.
        """
        # Are we even using AWS?
        if 'S3' in self.driver_name:
            try:
                # Yes? Is the boto package available?
                import boto
                # Shut the linter up.
                assert boto
                return True
            except ImportError:
                pass

        return False

    @property
    def guess_mimetype(self):
        """
        `True` if ckanext-cloudstorage is configured to guess mime types,
        `False` otherwise.
        """
        return p.toolkit.asbool(
            config.get('ckanext.cloudstorage.guess_mimetype', False)
        )


class ResourceCloudStorage(CloudStorage):
    def __init__(self, resource):
        """
        Support for uploading resources to any storage provider
        implemented by the apache-libcloud library.

        :param resource: The resource dict.
        """
        super(ResourceCloudStorage, self).__init__()

        self.filename = None
        self.old_filename = None
        self.file = None
        self.resource = resource

        upload_field_storage = resource.pop('upload', None)
        self._clear = resource.pop('clear_upload', None)
        multipart_name = resource.pop('multipart_name', None)

        # Check to see if a file has been provided
        if isinstance(upload_field_storage, (ALLOWED_UPLOAD_TYPES)):
            if len(upload_field_storage.filename):
                self.filename = munge.munge_filename(upload_field_storage.filename)
                self.file_upload = _get_underlying_file(upload_field_storage)
                resource['url'] = self.filename
                resource['url_type'] = 'upload'
                resource['last_modified'] = datetime.utcnow()
                # (canada fork only): filesize attribute
                self.file_upload.seek(0, os.SEEK_END)
                self.filesize = self.file_upload.tell()
                # go back to the beginning of the file buffer
                self.file_upload.seek(0, os.SEEK_SET)
        elif multipart_name and self.can_use_advanced_aws:
            # This means that file was successfully uploaded and stored
            # at cloud.
            # Currently implemented just AWS version
            resource['url'] = munge.munge_filename(multipart_name)
            resource['url_type'] = 'upload'
        elif self._clear and resource.get('id'):
            # Apparently, this is a created-but-not-commited resource whose
            # file upload has been canceled. We're copying the behaviour of
            # ckaenxt-s3filestore here.
            old_resource = model.Session.query(
                model.Resource
            ).get(
                resource['id']
            )

            self.old_filename = old_resource.url
            resource['url_type'] = ''

    def path_from_filename(self, rid, filename):
        """
        Returns a bucket path for the given resource_id and filename.

        :param rid: The resource ID.
        :param filename: The unmunged resource filename.
        """
        return os.path.join(
            'resources',
            rid,
            munge.munge_filename(filename)
        )

    def get_path(self, resource_id):
        # at this point, any auth should be done already as you
        # have to pass a Resource object to even get the uploader class (canada fork only)
        #TODO: upstream contribution??
        user = get_action('get_site_user')({'ignore_auth': True}, {})
        resource = get_action('resource_show')({"user": user['name']}, {'id': resource_id})
        filename = resource['url'].rsplit('/', 1)[-1]

        return self.get_url_from_filename(resource_id, filename)

    def upload(self, id, max_size=10):
        """
        Complete the file upload, or clear an existing upload.

        :param id: The resource_id.
        :param max_size: Ignored.
        """
        if self.filename:
            if self.can_use_advanced_azure:
                from azure.storage.blob import ContentSettings  # type: ignore
                from azure.storage.blob import BlobServiceClient

                svc_client = BlobServiceClient.from_connection_string(self.connection_link)
                container_client = svc_client.get_container_client(self.container_name)
                blob_client = container_client.get_blob_client(self.path_from_filename(
                                                                    id,
                                                                    self.filename
                                                                ))
                stream = self.file_upload
                blob_client.upload_blob(stream, overwrite=True)
                if self.guess_mimetype:
                    content_type, _ = mimetypes.guess_type(self.filename)
                    if not content_type:
                        content_type = 'application/octet-stream'
                    blob_client.set_http_headers(ContentSettings(content_type=content_type))
                return stream.tell()

            else:
                # If it's temporary file, we'd better convert it
                # into FileIO. Otherwise libcloud will iterate
                # over lines, not over chunks and it will really
                # slow down the process for files that consist of
                # millions of short linew
                if isinstance(self.file_upload, SpooledTemporaryFile):
                    self.file_upload.rollover()
                    try:
                        # extract underlying file
                        file_upload_iter = self.file_upload._file.detach()
                    except AttributeError:
                        # It's python2
                        file_upload_iter = self.file_upload._file
                else:
                    file_upload_iter = iter(self.file_upload)

                object_name = self.path_from_filename(id, self.filename)
                self.container.upload_object_via_stream(iterator=file_upload_iter,
                                                        object_name=object_name)

        elif self._clear and self.old_filename and not self.leave_files:
            # This is only set when a previously-uploaded file is replace
            # by a link. We want to delete the previously-uploaded file.
            try:
                self.container.delete_object(
                    self.container.get_object(
                        self.path_from_filename(
                            id,
                            self.old_filename
                        )
                    )
                )
            except ObjectDoesNotExistError:
                # It's possible for the object to have already been deleted, or
                # for it to not yet exist in a committed state due to an
                # outstanding lease.
                return

    def get_url_from_filename(self, rid, filename, content_type=None):
        """
        Retrieve a publically accessible URL for the given resource_id
        and filename.

        .. note::

            Works for Azure and any libcloud driver that implements
            support for get_object_cdn_url (ex: AWS S3).

        :param rid: The resource ID.
        :param filename: The resource filename.
        :param content_type: Optionally a Content-Type header.

        :returns: Externally accessible URL or None.
        """
        # Find the key the file *should* be stored at.
        path = self.path_from_filename(rid, filename)

        # If advanced azure features are enabled, generate a temporary
        # shared access link instead of simply redirecting to the file.
        if self.can_use_advanced_azure and self.use_secure_urls:
            from azure.storage.blob import BlobClient, BlobSasPermissions, BlobServiceClient, generate_blob_sas

            svc_client = BlobServiceClient.from_connection_string(self.connection_link)
            container_client = svc_client.get_container_client(self.container_name)
            blob_client = container_client.get_blob_client(path)
            permissions = BlobSasPermissions(read=True)
            token_expires = datetime.utcnow() + timedelta(hours=1)
            sas_token = generate_blob_sas(account_name=blob_client.account_name,
                                      account_key=blob_client.credential.account_key,
                                      container_name=blob_client.container_name,
                                      blob_name=blob_client.blob_name,
                                      permission=permissions,
                                      expiry=token_expires)

            blob_client = BlobClient(svc_client.url,
                                    container_name=blob_client.container_name,
                                    blob_name=blob_client.blob_name,
                                    credential=sas_token)

            # The url from blob_client above actually generate the url for download
            # but the file path is mixed with the filename e.g
            # we want to download `example.csv` but the url generate this
            # `resource12565-316r3example.csv` which is mung of the filename with its path
            # hence the below method enable the actual download of the file with its name
            url = 'https://{}.blob.core.windows.net/{}/{}?{}'.format(
                blob_client.account_name,
                self.container_name,
                path,
                sas_token
            )

            return url

        elif self.can_use_advanced_aws and self.use_secure_urls:

            from boto.s3.connection import S3Connection
            os.environ['S3_USE_SIGV4'] = 'True'
            s3_connection = S3Connection(
                aws_access_key_id=self.driver_options['key'],
                aws_secret_access_key=self.driver_options['secret'],
                security_token=self.driver_options['token'],
                host='s3.eu-west-1.amazonaws.com'
            )

            generate_url_params = {"expires_in": 60 * 60,
                                   "method": "GET",
                                   "bucket": self.container_name,
                                   "key": path}
            if content_type:
                generate_url_params['headers'] = {"Content-Type": content_type}

            return s3_connection.generate_url_sigv4(**generate_url_params)

        # Find the object for the given key.
        obj = self.container.get_object(path)
        if obj is None:
            return

        # Not supported by all providers!
        try:
            return self.driver.get_object_cdn_url(obj)
        except NotImplementedError:
            if 'S3' in self.driver_name:
                return urlparse.urljoin(
                    'https://' + self.driver.connection.host,
                    '{container}/{path}'.format(
                        container=self.container_name,
                        path=path
                    )
                )
            # This extra 'url' property isn't documented anywhere, sadly.
            # See azure_blobs.py:_xml_to_object for more.
            elif 'url' in obj.extra:
                return obj.extra['url']
            raise

    @property
    def package(self):
        return model.Package.get(self.resource['package_id'])
