# Copyright 2017 Sberbank
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from contextlib import contextmanager

from oslo_log import log as logging

from glance.common import utils
from glance.db.sqlalchemy import api
from glance.i18n import _LI


LOG = logging.getLogger(__name__)


def convert_proxy_to_metadata(proxy):
    visibility = getattr(proxy, 'visibility', 'private')
    size = proxy.size if proxy.size is not None else 0
    props = getattr(proxy, 'extra_properties', None)
    locations = getattr(proxy, 'locations', [])
    return {
        'size': size,
        'is_public': visibility == 'public',
        'owner': getattr(proxy, 'owner', None),
        'properties': {'image_type': props.get('image_type')} if props else {},
        'status': proxy.status,
        'location': locations[0] if locations else {}
    }


class QuotaDriver(object):
    """Quota Driver is responsible for managing quotas in Glance.
    It covers three main situations:
    1) create or update image (need to consider locations and public images)
    2) upload to glance
    3) delete
    """
    @classmethod
    def update(cls, context, image_id, image_meta):
        """Return quota manager for image update, this is mostly needed for
        monitoring public images and changing owner
        """
        active = image_meta['status'] == 'active'
        public = image_meta['is_public'] is True
        owner = image_meta.get('owner')
        has_reservations = api.quota_get_reservartions(context, image_id)
        if all([active, not public, owner, not has_reservations]):
            size = image_meta.get('size', 0)
            props = image_meta.get('properties')
            image_type = props.get('image_type') if props else None
            url = image_meta['location'].get('url')
            return QuotaReservationManager(context, image_id, size,
                                           url, owner, image_type)
        elif all([active, public, owner, has_reservations]):
            return release_manager(context, image_id)
        else:
            return noop()

    @classmethod
    def activate(cls, context, image_id, image_meta):
        """Return quota manager for image activation"""
        non_public = image_meta['is_public'] is False
        owner = image_meta.get('owner') or context.tenant
        has_reservations = api.quota_get_reservartions(context, image_id)
        if all([non_public, owner, not has_reservations]):
            # Quota will be requested only if it doesn't have any
            # reservations, image is not public and owner is known
            size = image_meta.get('size', 0)
            props = image_meta.get('properties')
            image_type = props.get('image_type') if props else None
            url = image_meta['location'].get('url')
            return QuotaReservationManager(context, image_id, size,
                                           url, owner, image_type)
        else:
            return noop()

    @classmethod
    def release(cls, context, image_id):
        """Return quota manager when delete image"""
        return release_manager(context, image_id)


@contextmanager
def noop():
    """Noop manager in case if there is no need for quota."""
    yield


@contextmanager
def release_manager(context, image_id):
    yield
    api.quota_clear_reservations(context, image_id)


class QuotaReservationManager(object):
    """Class is responsible for quota occupation during image activation.
    It is guaranteed that non-public images provided for quota activation.
    """

    def __init__(self, context, image_id, size, url, owner, image_type):
        self.context = context
        self.image_id = image_id
        self.applied_quotas = {'total_images_number': 1}

        size_condition = size and url and not url.startswith('http')
        if size_condition:
            self.applied_quotas['total_images_size'] = size
        if image_type == 'snapshot':
            self.applied_quotas['total_snapshots_number'] = 1
            if size_condition:
                self.applied_quotas['total_snapshots_size'] = size

        self.reservations = []
        self.scope = owner

    def __enter__(self):
        limits = api.quotas_get(self.context, self.scope)
        if not limits:
            # create limits if absent - in this case all non empty projects
            # always have quota value created
            # it allows not to recalculate quotas when updating defaults
            parent = utils.ProjectUtils.get_domain(self.scope)
            limits = api.quotas_get_defaults(self.context, parent)
            api.quotas_set(self.context, self.scope, limits)
        self.reservations = api.quota_create_reservations(
            self.context, self.image_id, self.scope, self.applied_quotas,
            limits)
        LOG.info(_LI("Quota reservation for scope %s "
                     "were successfully created"), self.scope)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            api.quota_delete_reservations(self.context, self.reservations)
            LOG.info(_LI("Delete quota reservation for scope %s"), self.scope)
        else:
            try:
                api.quota_commit_reservations(self.context, self.reservations)
                LOG.info(_LI("Quota reservation for scope %s were "
                             "successfully committed."), self.scope)
            except Exception:
                api.quota_delete_reservations(self.context, self.reservations)
                LOG.info(_LI("Delete quota reservation for scope %s"),
                         self.scope)
                raise
