# Copyright 2011 OpenStack Foundation
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


from glance.api.v1 import images
from glance.api.v1 import members
from glance.api.v2 import quotas
from glance.common import wsgi


class API(wsgi.Router):

    """WSGI router for Glance v1 API requests."""

    def __init__(self, mapper):
        reject_method_resource = wsgi.Resource(wsgi.RejectMethodController())

        images_resource = images.create_resource()

        mapper.connect("/",
                       controller=images_resource,
                       action="index")
        mapper.connect("/images",
                       controller=images_resource,
                       action='index',
                       conditions={'method': ['GET']})
        mapper.connect("/images",
                       controller=images_resource,
                       action='create',
                       conditions={'method': ['POST']})
        mapper.connect("/images",
                       controller=reject_method_resource,
                       action='reject',
                       allowed_methods='GET, POST')
        mapper.connect("/images/detail",
                       controller=images_resource,
                       action='detail',
                       conditions={'method': ['GET', 'HEAD']})
        mapper.connect("/images/detail",
                       controller=reject_method_resource,
                       action='reject',
                       allowed_methods='GET, HEAD')
        mapper.connect("/images/{id}",
                       controller=images_resource,
                       action="meta",
                       conditions=dict(method=["HEAD"]))
        mapper.connect("/images/{id}",
                       controller=images_resource,
                       action="show",
                       conditions=dict(method=["GET"]))
        mapper.connect("/images/{id}",
                       controller=images_resource,
                       action="update",
                       conditions=dict(method=["PUT"]))
        mapper.connect("/images/{id}",
                       controller=images_resource,
                       action="delete",
                       conditions=dict(method=["DELETE"]))
        mapper.connect("/images/{id}",
                       controller=reject_method_resource,
                       action='reject',
                       allowed_methods='GET, HEAD, PUT, DELETE')

        members_resource = members.create_resource()

        mapper.connect("/images/{image_id}/members",
                       controller=members_resource,
                       action="index",
                       conditions={'method': ['GET']})
        mapper.connect("/images/{image_id}/members",
                       controller=members_resource,
                       action="update_all",
                       conditions=dict(method=["PUT"]))
        mapper.connect("/images/{image_id}/members",
                       controller=reject_method_resource,
                       action='reject',
                       allowed_methods='GET, PUT')
        mapper.connect("/images/{image_id}/members/{id}",
                       controller=members_resource,
                       action="show",
                       conditions={'method': ['GET']})
        mapper.connect("/images/{image_id}/members/{id}",
                       controller=members_resource,
                       action="update",
                       conditions={'method': ['PUT']})
        mapper.connect("/images/{image_id}/members/{id}",
                       controller=members_resource,
                       action="delete",
                       conditions={'method': ['DELETE']})
        mapper.connect("/images/{image_id}/members/{id}",
                       controller=reject_method_resource,
                       action='reject',
                       allowed_methods='GET, PUT, DELETE')
        mapper.connect("/shared-images/{id}",
                       controller=members_resource,
                       action="index_shared_images")

        quotas_resource = quotas.create_resource()
        mapper.connect('/quota_classes',
                       controller=quotas_resource,
                       action='get_quota_classes',
                       body_reject=True,
                       conditions={'method': ['GET']})
        mapper.connect('/quota_classes',
                       controller=quotas_resource,
                       action='set_quota_classes',
                       conditions={'method': ['PUT']})
        mapper.connect('/quota_classes',
                       controller=reject_method_resource,
                       action='reject',
                       allowed_methods='GET, PUT')

        mapper.connect('/quotas/{scope}',
                       controller=quotas_resource,
                       action='get_quotas',
                       body_reject=True,
                       conditions={'method': ['GET']})
        mapper.connect('/quotas/{scope}',
                       controller=quotas_resource,
                       action='set_quotas',
                       conditions={'method': ['PUT']})
        mapper.connect('/quotas/{scope}',
                       controller=quotas_resource,
                       action='delete_quotas',
                       body_reject=True,
                       conditions={'method': ['DELETE']})
        mapper.connect('/quotas/{scope}',
                       controller=reject_method_resource,
                       action='reject',
                       allowed_methods='GET, PUT, DELETE')

        mapper.connect('/quotas/{scope}/usage',
                       controller=quotas_resource,
                       action='quota_usage',
                       body_reject=True,
                       conditions={'method': ['GET']})

        super(API, self).__init__(mapper)
