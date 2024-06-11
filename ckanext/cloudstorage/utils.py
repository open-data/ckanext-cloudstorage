# -*- coding: utf-8 -*-
from __future__ import annotations
import mimetypes

import os.path
import tempfile

import ckan.lib.helpers as h
import ckan.plugins.toolkit as tk
from ckan import model
from ckan.lib import base, uploader
from ckanapi import LocalCKAN
from werkzeug.datastructures import FileStorage as FakeFileStorage

from ckanext.cloudstorage.storage import CloudStorage, ResourceCloudStorage

# (canada fork only): add more utility commands
import click
from sqlalchemy import and_ as _and_
from ckan.lib.munge import munge_filename
from azure.storage.blob import ContentSettings  # type: ignore
from azure.storage.blob import BlobServiceClient  # type: ignore


def fix_cors(domains):
    cs = CloudStorage()

    if cs.can_use_advanced_azure:
        from azure.storage import CorsRule
        from azure.storage import blob as azure_blob

        blob_service = azure_blob.BlockBlobService(
            cs.driver_options["key"], cs.driver_options["secret"]
        )

        blob_service.set_blob_service_properties(
            cors=[CorsRule(allowed_origins=domains, allowed_methods=["GET"])]
        )
        return "Done!", True
    else:
        return (
            "The driver {driver_name} being used does not currently"
            " support updating CORS rules through"
            " cloudstorage.".format(driver_name=cs.driver_name),
            False,
        )


def migrate(path, single_id):
    if not os.path.isdir(path):
        print("The storage directory cannot be found.")
        return

    lc = LocalCKAN()
    resources = {}
    failed: list[str] = []

    # The resource folder is stuctured like so on disk:
    # - storage/
    #   - ...
    # - resources/
    #   - <3 letter prefix>
    #     - <3 letter prefix>
    #       - <remaining resource_id as filename>
    #       ...
    #     ...
    #   ...
    for root, dirs, files in os.walk(path):
        # Only the bottom level of the tree actually contains any files. We
        # don't care at all about the overall structure.
        if not files:
            continue

        split_root = root.split("/")
        resource_id = split_root[-2] + split_root[-1]

        for file_ in files:
            ckan_res_id = resource_id + file_
            if single_id and ckan_res_id != single_id:
                continue

            resources[ckan_res_id] = os.path.join(root, file_)

    for i, resource in enumerate(iter(list(resources.items())), 1):
        resource_id, file_path = resource
        print(
            "[{i}/{count}] Working on {id}".format(
                i=i, count=len(resources), id=resource_id
            )
        )
        try:
            resource = lc.action.resource_show(id=resource_id)
        except tk.ObjectNotFound:
            print("\tResource not found")
            continue
        if resource["url_type"] != "upload":
            print("\t`url_type` is not `upload`. Skip")
            continue

        with open(file_path, "rb") as fin:
            resource["upload"] = FakeFileStorage(
                fin, resource["url"].split("/")[-1]
            )
            try:
                uploader = ResourceCloudStorage(resource)
                uploader.upload(resource["id"])
            except Exception as e:
                failed.append(resource_id)
                print(
                    "\tError of type {0} during upload: {1}".format(type(e), e)
                )

    if failed:
        log_file = tempfile.NamedTemporaryFile(delete=False)
        log_file.file.writelines([l.encode() for l in failed])
        print(
            "ID of all failed uploads are saved to `{0}`: {1}".format(
                log_file.name, failed
            )
        )


def resource_download(id, resource_id, filename=None):
    context = {
        "model": model,
        "session": model.Session,
        "user": tk.c.user or tk.c.author,
        "auth_user_obj": tk.c.userobj,
    }

    try:
        resource = tk.get_action("resource_show")(context, {"id": resource_id})
    except tk.ObjectNotFound:
        return base.abort(404, tk._("Resource not found"))
    except tk.NotAuthorized:
        return base.abort(
            401, tk._("Unauthorized to read resource {0}".format(id))
        )

    # This isn't a file upload, so either redirect to the source
    # (if available) or error out.
    if resource.get("url_type") != "upload":
        url = resource.get("url")
        if not url:
            return base.abort(404, tk._("No download is available"))
        return h.redirect_to(url)

    if filename is None:
        # No filename was provided so we'll try to get one from the url.
        filename = os.path.basename(resource["url"])

    upload = uploader.get_resource_uploader(resource)

    # if the client requests with a Content-Type header (e.g. Text preview)
    # we have to add the header to the signature
    content_type = getattr(tk.request, "content_type", None)
    if not content_type:
        content_type, _enc = mimetypes.guess_type(filename)

    uploaded_url = upload.get_url_from_filename(
        resource["id"], filename, content_type=content_type
    )

    # The uploaded file is missing for some reason, such as the
    # provider being down.
    if uploaded_url is None:
        return base.abort(404, tk._("No download is available"))

    return h.redirect_to(uploaded_url)


# (canada fork only): add more utility commands
def migrate_file(file_path, resource_id):
    if not os.path.isfile(file_path):
        print('The file path is not a file.')
        return

    failed = []

    try:
        resource = tk.get_action('resource_show')({'ignore_auth': True}, {'id': resource_id})
    except tk.ObjectNotFound:
        print(u'Resource not found')
        return

    if resource['url_type'] != 'upload':
        print(u'`url_type` is not `upload`.')
        return

    with open(file_path, 'rb') as fin:
        resource['upload'] = FakeFileStorage(
            fin,
            resource['url'].split('/')[-1]
        )
        try:
            uploader = ResourceCloudStorage(resource)
            uploader.upload(resource['id'])
            head, tail = os.path.split(file_path)
            print(u'Uploaded file {0} successfully for resource {1}.'.format(
                    tail, resource_id))
        except Exception as e:
            failed.append(resource_id)
            print(u'Error of type {0} during upload: {1}'.format(type(e), e))

    if failed:
        log_file = tempfile.NamedTemporaryFile(delete=False)
        log_file.file.writelines(failed)
        print(u'ID of all failed uploads are saved to `{0}`'.format(log_file.name))


# (canada fork only): add more utility commands
def _get_uploads(get_linked = True, return_upload_objects_only = False):
    # type: (bool, bool) -> tuple[float, list]
    cs = CloudStorage()

    resource_urls = set(os.path.join(
                        u'resources',
                        id,
                        munge_filename(filename))
                    for id, filename in
                    model.Session.query(
                        model.Resource.id,
                        model.Resource.url) \
                        .join(model.Package,
                              model.Resource.package_id == model.Package.id) \
                        .filter(_and_(model.Resource.url_type == u'upload',
                                      model.Resource.state == model.core.State.ACTIVE,
                                      model.Package.state == model.core.State.ACTIVE)) \
                        .all())

    uploads = cs.container.list_objects()

    parsed_uploads = []
    total_space_used = 0
    for upload in uploads:
        if (upload.name in resource_urls
            if get_linked else
            upload.name not in resource_urls):

            if return_upload_objects_only:
                parsed_uploads.append(upload)
                continue

            resource_id = upload.name.split('/')[1]
            resource_fields = None
            if resource_id:
                resource_fields = model.Session.query(
                                    model.Resource.id,
                                    model.Resource.url,
                                    model.Resource.package_id,
                                    model.Resource.created,
                                    model.Resource.last_modified,
                                    model.Package.owner_org,
                                    model.Package.state,
                                    model.Resource.state) \
                                    .join(model.Package, model.Resource.package_id == model.Package.id) \
                                    .filter(_and_(model.Resource.url_type == u'upload',
                                                  model.Resource.id == resource_id)) \
                                    .first()
            parsed_uploads.append({
                u'resource_id': resource_fields[0] if resource_fields else None,
                u'resource_filename': resource_fields[1] if resource_fields else None,
                u'package_id': resource_fields[2] if resource_fields else None,
                u'created': h.render_datetime(resource_fields[3]) if resource_fields else None,
                u'last_modified': h.render_datetime(resource_fields[4]) if resource_fields else None,
                u'organization_id': resource_fields[5] if resource_fields else None,
                u'upload_url': upload.name,
                u'upload_size': upload.size / 1000.0,
                u'package_state': resource_fields[6] if resource_fields else None,
                u'resource_state': resource_fields[7] if resource_fields else None})
            total_space_used += upload.size / 1000.0

    return total_space_used, parsed_uploads


# (canada fork only): add more utility commands
def _humanize_space(space):
    # type: (float) -> tuple[float, str]
    parsed_space = space
    for unit in ['KB', 'MB', 'GB', 'TB']:
        if parsed_space < 1000.0:
            return parsed_space, unit
        parsed_space /= 1000.0
    return space, 'KB'


# (canada fork only): add more utility commands
def _write_uploads_to_csv(output_path, uploads):
    #type: (str, list) -> None
    if not uploads:
        click.echo(u"Nothing to write to {}".format(output_path))
        return
    with open(output_path, u'w') as f:
        w = csv.writer(f, encoding='utf-8')
        w.writerow((u'resource_id',
                    u'package_id',
                    u'organization_id',
                    u'resource_filename',
                    u'upload_url',
                    u'upload_file_size_in_kb',
                    u'resource_created',
                    u'resource_last_modified',
                    u'package_state',
                    u'resource_state'))
        for upload in uploads:
            w.writerow((
                upload[u'resource_id'],
                upload[u'package_id'],
                upload[u'organization_id'],
                upload[u'resource_filename'],
                upload[u'upload_url'],
                upload[u'upload_size'],
                upload[u'created'],
                upload[u'last_modified'],
                upload[u'package_state'],
                upload[u'resource_state']))
        click.echo(u"Wrote {} row(s) to {}"
                    .format(len(uploads), output_path))


# (canada fork only): add more utility commands
def list_linked_uploads(output_path):
    # type: (str|None) -> None
    used_space, good_uploads = _get_uploads()

    if output_path:
        _write_uploads_to_csv(output_path, good_uploads)
    else:
        used_space, unit = _humanize_space(used_space)
        click.echo(u"Found {} uploads(s) with linked resources. Total space: {} {}."
                    .format(len(good_uploads), used_space, unit))


# (canada fork only): add more utility commands
def list_unlinked_uploads(output_path):
    # type: (str|None) -> None
    used_space, uploads_missing_resources = _get_uploads(get_linked = False)

    if output_path:
        _write_uploads_to_csv(output_path, uploads_missing_resources)
    else:
        used_space, unit = _humanize_space(used_space)
        click.echo(u"Found {} upload(s) with missing or deleted resources. Total space: {} {}."
                    .format(len(uploads_missing_resources), used_space, unit))


# (canada fork only): add more utility commands
def remove_unlinked_uploads():
    cs = CloudStorage()

    used_space, uploads_missing_resources = _get_uploads(get_linked = False, return_upload_objects_only = True)

    num_success = 0
    num_failures = 0
    saved_space = 0
    for upload in uploads_missing_resources:
        if cs.container.delete_object(upload):
            click.echo(u"Deleted {}".format(upload.name))
            num_success += 1
            saved_space += upload.size / 1000.0
            used_space -= upload.size / 1000.0
        else:
            click.echo(u"Failed to delete {}".format(upload.name))
            num_failures += 1

    if num_success:
        saved_space, unit = _humanize_space(used_space)
        click.echo(u"Deleted {} upload(s). Saved {} {}."
                    .format(num_success, saved_space, unit))

    if num_failures:
        click.echo(u"Failed to delete {} upload(s).".format(num_failures))

    if used_space:
        used_space, unit = _humanize_space(used_space)
        click.echo(u"Remaining space used by unlinked uploads: {} {}."
                    .format(used_space, unit))


# (canada fork only): add more utility commands
def list_missing_uploads(output_path):
    # type: (str|None) -> None
    cs = CloudStorage()

    upload_urls = set(u.name for u in cs.container.list_objects())

    resource_fields = model.Session.query(
                        model.Resource.id,
                        model.Resource.url,
                        model.Resource.package_id,
                        model.Resource.created,
                        model.Resource.last_modified,
                        model.Package.owner_org) \
                        .join(model.Package,
                              model.Resource.package_id == model.Package.id) \
                        .filter(_and_(model.Resource.url_type == u'upload',
                                      model.Resource.state == model.core.State.ACTIVE,
                                      model.Package.state == model.core.State.ACTIVE)) \
                        .all()

    resources_missing_uploads = []
    for id, filename, package_id, created, last_modified, organization_id in resource_fields:
        url = os.path.join(
                u'resources',
                id,
                munge_filename(filename))

        if url not in upload_urls:
            resources_missing_uploads.append({
                u'resource_id': id,
                u'resource_filename': filename,
                u'package_id': package_id,
                u'created': h.render_datetime(created),
                u'last_modified': h.render_datetime(last_modified),
                u'organization_id': organization_id,
                u'upload_url': None,
                u'upload_size': None,
                u'package_state': model.core.State.ACTIVE,
                u'resource_state': model.core.State.ACTIVE})

    if output_path:
        _write_uploads_to_csv(output_path, resources_missing_uploads)
    else:
        click.echo(u"Found {} resource(s) with missing uploads."
                    .format(len(resources_missing_uploads)))


# (canada fork only): add more utility commands
def reguess_mimetypes(resource_id=None, verbose=False):
    # type: (str|None, bool) -> None
    lc = LocalCKAN()

    if resource_id:
        resource_fields = [(resource_id, None)]
    else:
        resource_fields = model.Session.query(model.Resource.id,
                                            model.Resource.package_id) \
                            .join(model.Package,
                                model.Resource.package_id == model.Package.id) \
                            .filter(_and_(model.Resource.url_type == u'upload',
                                        model.Resource.state == model.core.State.ACTIVE,
                                        model.Package.state == model.core.State.ACTIVE)) \
                            .all()

    total = len(resource_fields)
    count = 0
    success = 0
    failed = 0

    click.echo(u'Reguessing ~{} resource formats.'.format(total))

    for resource_id, package_id in resource_fields:
        try:
            resource = lc.action.resource_show(id=resource_id)
        except tk.ObjectNotFound:
            if verbose:
                click.echo(u'Could not find resource {}. Skipping...'.format(resource_id))
            continue

        count += 1

        resource['upload'] = FakeFileStorage(  # resource object does not have an upload at this point, fake it.
            resource['url'].split('/')[-1],  # we do not need the actual file data for this.
            resource['url'].split('/')[-1]
        )

        try:
            uploader = ResourceCloudStorage(resource)
            if uploader.filename and uploader.can_use_advanced_azure:
                svc_client = BlobServiceClient.from_connection_string(uploader.connection_link)
                container_client = svc_client.get_container_client(uploader.container_name)
                blob_client = container_client.get_blob_client(
                    uploader.path_from_filename(resource['id'], uploader.filename))
                content_type, _ = mimetypes.guess_type(uploader.filename)
                if not content_type:
                    content_type = 'application/octet-stream'
                blob_client.set_http_headers(ContentSettings(content_type=content_type))
                if verbose:
                    click.echo(u'Reguessed mimetype successfully for resource {1}.'.format(resource_id))
                success += 1
                click.echo(u'\t{}/~{}'.format(count,total))
        except Exception as e:
            failed += 1
            if verbose:
                click.echo(u'Failed to reguess mimetype: {0} - {1}'.format(type(e), e))
            click.echo(u'\t{}/~{}'.format(count,total))

    click.echo(u'Successfully reguessed {}/{} resource formats.'.format(success, count))
    click.echo(u'Failed to reguess {}/{} resource formats.'.format(failed, count))
