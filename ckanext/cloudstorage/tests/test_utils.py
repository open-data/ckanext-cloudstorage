# -*- coding: utf-8 -*-
from urllib.parse import urlparse

import ckan.plugins.toolkit as tk
import pytest
from ckan.tests import factories, helpers

from ckanext.cloudstorage import storage, utils


@pytest.mark.usefixtures("with_driver_options", "with_plugins", "clean_db")
class TestResourceDownload(object):
    def test_utils_used_by_download_route(self, app, mocker):
        url = tk.url_for("resource.download", id="a", resource_id="b")
        mocker.patch(
            "ckanext.cloudstorage.utils.resource_download", return_value=""
        )
        app.get(url)
        utils.resource_download.assert_called_once_with("a", "b", None)

    def test_status_codes(self, app):
        user = factories.User()
        org = factories.Organization()
        dataset = factories.Dataset(private=True, owner_org=org["id"])
        resource = factories.Resource(package_id=dataset["id"])

        env = {"REMOTE_USER": user["name"]}
        url = tk.url_for("resource.download", id="a", resource_id="b")
        app.get(url, status=404, extra_environ=env)

        url = tk.url_for(
            "resource.download", id=dataset["id"], resource_id=resource["id"]
        )
        app.get(url, status=401, extra_environ=env)

        helpers.call_action("package_patch", id=dataset["id"], private=False)
        app.get(url, status=302, extra_environ=env, follow_redirects=False)

    def test_download(self, create_with_upload, app):
        filename = "file.txt"
        resource = create_with_upload(
            "hello world", filename, package_id=factories.Dataset()["id"]
        )
        url = tk.url_for(
            "resource.download",
            id=resource["package_id"],
            resource_id=resource["id"],
        )
        resp = app.get(url, status=302, follow_redirects=False)

        uploader = storage.ResourceCloudStorage(resource)
        expected_url = uploader.get_url_from_filename(resource["id"], filename)
        assert (
            urlparse(resp.headers["location"]).path
            == urlparse(expected_url).path
        )
