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

import os
import signal
import uuid

from oslo_serialization import jsonutils
import requests

from glance.api.v2 import quotas
from glance.tests import functional
from glance.tests import utils as test_utils


TENANT1 = str(uuid.uuid4())
TENANT2 = str(uuid.uuid4())
DOMAIN = 'default'


class TestImages(functional.FunctionalTest):

    def setUp(self):
        super(TestImages, self).setUp()
        self.cleanup()
        self.api_server.deployment_flavor = 'noauth'
        self.api_server.data_api = 'glance.db.sqlalchemy.api'
        for i in range(3):
            ret = test_utils.start_http_server("foo_image_id%d" % i,
                                               "foo_image%d" % i)
            setattr(self, 'http_server%d_pid' % i, ret[0])
            setattr(self, 'http_port%d' % i, ret[1])

    def tearDown(self):
        for i in range(3):
            pid = getattr(self, 'http_server%d_pid' % i, None)
            if pid:
                os.kill(pid, signal.SIGKILL)

        super(TestImages, self).tearDown()

    def _url(self, path):
        return 'http://127.0.0.1:%d%s' % (self.api_port, path)

    def _get_quota_example(self):
        return {name: -1 for name in quotas.quota_classes}

    def _headers(self, custom_headers=None):
        base_headers = {
            'X-Identity-Status': 'Confirmed',
            'X-Auth-Token': '932c5c84-02ac-4fe5-a9ba-620af0e2bb96',
            'X-User-Id': 'f9a41d13-0c13-47e9-bee2-ce4e8bfe958e',
            'X-Tenant-Id': TENANT1,
            'X-Roles': 'member',
            'content-type': 'application/json',
        }
        base_headers.update(custom_headers or {})
        return base_headers

    def _create_image(self, custom_props=None):
        images_path = self._url('/v2/images')
        image_data = {'name': 'image-1', 'type': 'kernel',
                      'foo': 'bar', 'disk_format': 'aki',
                      'container_format': 'aki'}
        if custom_props:
            image_data.update(custom_props)
        image_data = jsonutils.dumps(image_data)
        response = requests.post(images_path, headers=self._headers(),
                                 data=image_data)
        self.assertEqual(201, response.status_code)
        image = jsonutils.loads(response.text)
        return image['id']

    def _upload_data(self, image_id, data, status=204):
        path = self._url('/v2/images/%s/file' % image_id)
        headers = self._headers({'content-type': 'application/octet-stream'})
        response = requests.put(path, headers=headers, data=data)
        self.assertEqual(status, response.status_code, response.text)

    def _update_image(self, image_id, data, status=200):
        path = self._url('/v2/images/%s' % image_id)
        media_type = 'application/openstack-images-v2.0-json-patch'
        headers = self._headers({'content-type': media_type})
        data = jsonutils.dumps(data)
        response = requests.patch(path, headers=headers, data=data)
        self.assertEqual(status, response.status_code, response.text)
        return response

    def _get_usage(self, scope):
        usage_path = self._url('/v2/quotas/%s/usage' % scope)
        return jsonutils.loads(requests.get(
            usage_path, headers=self._headers()).text)['usage']

    def _v1_headers(self, name, public=False):
        headers = {
            'content-type': 'application/octet-stream',
            'X-Image-Meta-Name': name,
            'X-Image-Meta-disk_format': 'raw',
            'X-Image-Meta-container_format': 'ovf',
        }
        if public:
            headers['X-Image-Meta-Is-Public'] = 'True'

        return self._headers(custom_headers=headers)

    def test_quotas_lifecycle(self):
        self.api_server.show_multiple_locations = True
        self.start_servers(**self.__dict__.copy())
        # test quota classes lifecycle
        path = self._url('/v2/quota_classes')
        response = requests.get(path, headers=self._headers())
        self.assertEqual(200, response.status_code)
        classes = jsonutils.loads(response.text)['quota_classes']
        self.assertEqual(self._get_quota_example(), classes)

        classes['total_images_number'] = 2
        classes['total_images_size'] = 2000
        classes['total_snapshots_number'] = 2
        classes['total_snapshots_size'] = 2000
        data = jsonutils.dumps({'quota_classes': classes})
        response = requests.put(
            path, headers=self._headers({'content-type': 'application/json'}),
            data=data)
        self.assertEqual(200, response.status_code)
        upd_classes = jsonutils.loads(response.text)['quota_classes']
        self.assertEqual(2, upd_classes['total_images_number'])

        response = requests.get(path, headers=self._headers())
        classes_check = jsonutils.loads(response.text)['quota_classes']
        self.assertEqual(classes, classes_check)

        # test domain specification
        domain_path = self._url('/v2/quotas/default')
        response = requests.get(domain_path, headers=self._headers())
        self.assertEqual(200, response.status_code)
        domain_check = jsonutils.loads(response.text)['quotas']
        for n in domain_check:
            self.assertEqual(-1, domain_check[n])

        domain_quotas = {
            'total_images_number': 3,
            'total_images_size': 2000,
            'total_snapshots_number': 3,
            'total_snapshots_size': 1000
        }
        data = jsonutils.dumps({'quotas': domain_quotas})
        response = requests.put(domain_path, headers=self._headers(),
                                data=data)
        self.assertEqual(200, response.status_code)
        domain_update_check = jsonutils.loads(response.text)['quotas']
        self.assertEqual(domain_quotas, domain_update_check)

        # test project defaults consider defaults and parent domain
        project_path = self._url('/v2/quotas/%s' % TENANT1)
        response = requests.get(project_path, headers=self._headers())
        self.assertEqual(200, response.status_code)
        project_defaults_check = jsonutils.loads(response.text)['quotas']
        self.assertEqual(1000, project_defaults_check['total_snapshots_size'])

        # define project quotas
        project_quotas = {
            'total_images_number': 5,
            'total_images_size': 2000,
            'total_snapshots_number': 2,
            'total_snapshots_size': 1000
        }
        # ensure we cannot overbook domain quotas
        data = jsonutils.dumps({'quotas': project_quotas})
        response = requests.put(project_path, headers=self._headers(),
                                data=data)
        self.assertEqual(400, response.status_code)

        project_quotas['total_images_number'] = 2
        data = jsonutils.dumps({'quotas': project_quotas})
        response = requests.put(project_path, headers=self._headers(),
                                data=data)
        self.assertEqual(200, response.status_code)
        project_quota_check = jsonutils.loads(response.text)['quotas']
        self.assertEqual(project_quotas, project_quota_check)

        requests.delete(project_path, headers=self._headers())
        response = requests.get(project_path, headers=self._headers())
        self.assertEqual(200, response.status_code)
        project_delete_check = jsonutils.loads(response.text)['quotas']
        self.assertEqual(project_defaults_check, project_delete_check)

        # ensure usage does work for project and forbidden for domains
        usage_path = self._url('/v2/quotas/%s/usage' % TENANT1)
        response = requests.get(usage_path, headers=self._headers())
        self.assertEqual(200, response.status_code)
        project_usage_check = jsonutils.loads(response.text)['usage']
        for n in project_usage_check:
            self.assertEqual(0, project_usage_check[n]['usage'])

        response = requests.get(self._url('/v2/quotas/default/usage'),
                                headers=self._headers())
        self.assertEqual(403, response.status_code)

        # test occupation with v2
        image1_id = self._create_image()
        # ensure usage is not considering this image
        usage_non_active = self._get_usage(TENANT1)
        self.assertEqual(0, usage_non_active['total_images_number']['usage'])

        # activate image
        self._upload_data(image1_id, "Z" * 5)
        usage_image1 = self._get_usage(TENANT1)
        self.assertEqual(1, usage_image1['total_images_number']['usage'])
        self.assertEqual(5, usage_image1['total_images_size']['usage'])

        # create snapshot image
        image2_id = self._create_image({'image_type': 'snapshot'})
        self._upload_data(image2_id, "Z" * 1000)
        usg_with_snapshot = self._get_usage(TENANT1)
        self.assertEqual(2, usg_with_snapshot['total_images_number']['usage'])
        self.assertEqual(1,
                         usg_with_snapshot['total_snapshots_number']['usage'])
        self.assertEqual(1005, usg_with_snapshot['total_images_size']['usage'])
        self.assertEqual(1000,
                         usg_with_snapshot['total_snapshots_size']['usage'])
        # break the quota
        image3_id = self._create_image()
        self._upload_data(image3_id, "Z" * 1000, status=403)
        # ensure status is queued again
        image_status = jsonutils.loads(
            requests.get(self._url('/v2/images/%s' % image3_id),
                         headers=self._headers()).text)['status']
        self.assertEqual('queued', image_status)

        # publish image 2 so free up the quota
        self._update_image(image2_id,
                           [{"replace": "/visibility", "value": "public"}])
        usg_with_public = self._get_usage(TENANT1)
        self.assertEqual(1, usg_with_public['total_images_number']['usage'])
        self.assertEqual(0,
                         usg_with_public['total_snapshots_number']['usage'])
        self.assertEqual(5, usg_with_public['total_images_size']['usage'])
        self.assertEqual(0,
                         usg_with_public['total_snapshots_size']['usage'])

        self._upload_data(image3_id, "Z" * 1000)
        self._update_image(image2_id,
                           [{"replace": "/visibility", "value": "private"}],
                           status=403)

        # ensure delete free up the quota
        requests.delete(self._url('/v2/images/%s' % image3_id),
                        headers=self._headers())
        usg_with_deleted = self._get_usage(TENANT1)
        self.assertEqual(1, usg_with_deleted['total_images_number']['usage'])
        self.assertEqual(0,
                         usg_with_deleted['total_snapshots_number']['usage'])
        self.assertEqual(5, usg_with_deleted['total_images_size']['usage'])
        self.assertEqual(0,
                         usg_with_deleted['total_snapshots_size']['usage'])

        # ensure we can make private images again
        self._update_image(image2_id,
                           [{"replace": "/visibility", "value": "private"}])

        v1_image_data = "Z" * 1000
        v1_path = self._url("/v1/images")
        # ensure we can create public images with v1
        response = requests.post(v1_path,
                                 headers=self._v1_headers("test_public",
                                                          public=True),
                                 data=v1_image_data)
        self.assertEqual(201, response.status_code, response.text)

        # try to break quota with v1
        response = requests.post(v1_path, headers=self._v1_headers("test"),
                                 data=v1_image_data)
        self.assertEqual(403, response.status_code, response.text)

        # free up the quota with v1
        requests.delete(self._url('/v1/images/%s' % image2_id),
                        headers=self._headers())
        requests.delete(self._url('/v1/images/%s' % image1_id),
                        headers=self._headers())

        usg_final = self._get_usage(TENANT1)
        self.assertEqual(0, usg_final['total_images_number']['usage'])
        self.assertEqual(0, usg_final['total_snapshots_number']['usage'])
        self.assertEqual(0, usg_final['total_images_size']['usage'])
        self.assertEqual(0, usg_final['total_snapshots_size']['usage'])
