# -*- coding: utf-8 -*-

import ckan.plugins as p
from routes.mapper import SubMapper


class MixinPlugin(p.SingletonPlugin):
    p.implements(p.IRoutes, inherit=True)

    # IRoutes

    def before_map(self, map):
        sm = SubMapper(
            map, controller="ckanext.cloudstorage.controller:StorageController"
        )

        # Override the resource download controllers so we can do our
        # lookup with libcloud.
        with sm:
            sm.connect(
                "resource_download",
                "/dataset/{id}/resource/{resource_id}/download",
                action="resource_download",
            )
            sm.connect(
                "resource_download",
                "/dataset/{id}/resource/{resource_id}/download/{filename}",
                action="resource_download",
            )

        return map
