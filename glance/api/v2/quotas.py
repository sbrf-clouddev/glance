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
import sys

from oslo_config import cfg
from oslo_serialization import jsonutils
import six
from webob import exc as webob_exc

from glance.api import policy
from glance.common import exception
from glance.common import utils
from glance.common import wsgi
import glance.db
import glance.gateway
from glance.i18n import _
import glance.notifier
import glance.schema


CONF = cfg.CONF

quota_classes = ['total_images_number', 'total_images_size',
                 'total_snapshots_number', 'total_snapshots_size']


class RequestDeserializer(wsgi.JSONRequestDeserializer):
    def __init__(self):
        super(RequestDeserializer, self).__init__()
        self.class_schema = get_quota_class_schema()
        self.quota_schema = get_quota_schema()

    def _get_request_body(self, request):
        output = super(RequestDeserializer, self).default(request)
        if 'body' not in output:
            msg = _('Body expected in request.')
            raise webob_exc.HTTPBadRequest(explanation=msg)
        return output['body']

    @staticmethod
    def _check_all_classes_present(q):
        names = {c for c in q}
        diff = {c for c in quota_classes} - names
        if diff:
            raise webob_exc.HTTPBadRequest(
                "The following quotas are not found in update request: "
                "%s" % str(diff))

    def set_quota_classes(self, request):
        try:
            request.get_content_type(('application/json',))
        except exception.InvalidContentType as e:
            raise webob_exc.HTTPUnsupportedMediaType(explanation=e.msg)

        q_classes = self._get_request_body(request)
        try:
            self.class_schema.validate(q_classes)
            cls = q_classes['quota_classes']
        except exception.InvalidObject as e:
            raise webob_exc.HTTPBadRequest(explanation=e.msg)

        self._check_all_classes_present(cls)
        return {'quota_classes': cls}

    def set_quotas(self, request):
        try:
            request.get_content_type(('application/json',))
        except exception.InvalidContentType as e:
            raise webob_exc.HTTPUnsupportedMediaType(explanation=e.msg)

        quotas = self._get_request_body(request)
        try:
            self.quota_schema.validate(quotas)
            qs = quotas['quotas']
        except exception.InvalidObject as e:
            raise webob_exc.HTTPBadRequest(explanation=e.msg)

        self._check_all_classes_present(qs)
        return {'quotas': qs}


class QuotaController(object):
    """Manages operations on tasks."""

    def __init__(self):
        self.db_api = glance.db.get_api()
        self.policy = policy.Enforcer()

    def get_quota_classes(self, request):
        """Get quota classes with default values that will be used for new
        empty projects. Once we got some images for project or domain we
        need to instantiate project quota from defaults.
        """

        self.policy.enforce(request.context, 'get_quota_classes', {})
        quota_classes = self.db_api.quota_classes_get(request.context)
        return {'quota_classes': quota_classes}

    def set_quota_classes(self, request, quota_classes):
        """Set quota classes default values used for new projects"""

        self.policy.enforce(request.context, 'set_quota_classes', {})
        updates = self.db_api.quota_classes_set(request.context,
                                                quota_classes)
        return {'quota_classes': updates}

    @staticmethod
    def _get_domain(scope):
        domain = utils.ProjectUtils.get_domain(scope)
        if domain is None and utils.ProjectUtils.is_domain(scope) is False:
            raise webob_exc.HTTPBadRequest("Can't find domain or project "
                                           "for scope %s in Keystone when "
                                           "specifying quotas."
                                           % scope)
        return domain

    def get_quotas(self, request, scope):
        """Request quotas from database.
        If quotas is not present then return defaults.
        Domains by default are not restricted.
        """

        self.policy.enforce(request.context, 'get_quotas', {'scope': scope})
        # try to request quotas directly first
        quotas = self.db_api.quotas_get(request.context, scope)
        if not quotas:
            # 1. define if we specify quotas for domain or project
            domain = self._get_domain(scope)
            if domain:
                # for project we use defaults but we need to be sure
                # that defaults do not overbook domain quotas
                # otherwise defaults will be set to domain quotas
                quotas = self.db_api.quotas_get_defaults(request.context,
                                                         domain)
            else:
                quotas = {name: -1 for name in quota_classes}

        return {'quotas': quotas}

    def set_quotas(self, request, scope, quotas):
        self.policy.enforce(request.context, 'set_quotas',
                            {'scope': scope})
        # 1. calculate restrictions
        max_limit = {name: -1 for name in quota_classes}
        min_limit = {name: 0 for name in quota_classes}
        reasons = {name: {"max": "N/A", "min": "N/A"}
                   for name in quota_classes}
        # first we need to define if we have domain or project
        domain = self._get_domain(scope)
        if domain:
            # for project we request domain quotas if present
            for name, value in six.iteritems(
                    self.db_api.quotas_get(
                        context=request.context, scope=domain)):
                max_limit[name] = value
                reasons[name]["max"] = "parent domain %s" % domain
            # low bound limits restricted by current project usage
            for name, value in six.iteritems(self.db_api.quota_get_usage(
                    context=request.context, scope=scope)):
                min_limit[name] = value
                reasons[name]["min"] = "resource usage for project %s" % scope
        else:
            # for domains min values restricted by child projects
            projects = utils.ProjectUtils.get_projects(scope)
            child_quotas = {p.id: self.db_api.quotas_get(
                context=request.context, scope=p.id) for p in projects}
            for p, q in six.iteritems(child_quotas):
                for ch_k, ch_v in six.iteritems(q):
                    if min_limit[ch_k] < ch_v:
                        min_limit[ch_k] = ch_v
                        reasons[ch_k]["min"] = (
                            "quota value from project %s" % p)

            # also we calculate usages here as lower bound
            child_usages = [self.db_api.quota_get_usage(
                request.context, p.id) for p in projects]
            total_usages_by_classes = {name: 0 for name in quota_classes}
            for us in child_usages:
                for us_k, us_v in six.iteritems(us):
                    total_usages_by_classes[us_k] += us_v

            # check if current limits is more than usage
            for res_k, res_v in six.iteritems(min_limit):
                if min_limit[res_k] < total_usages_by_classes[res_k]:
                    min_limit[res_k] = total_usages_by_classes[res_k]
                    reasons[res_k]["min"] = ("overall usage for "
                                             "domain %s" % scope)

        # 2. validate restrictions
        for name, limit in six.iteritems(quotas):
            lmax = max_limit[name]
            lmin = min_limit[name]
            if lmax != -1 and limit > lmax or limit != -1 and limit < lmin:
                msg = (_("Incorrect quota value for %(cls)s. "
                         "Quota value (%(cur_value)s) must be between "
                         "min(%(min_value)s) and max(%(max_value)s). "
                         "Max value got from: %(max_reason)s. "
                         "Min value got from: %(min_reason)s.") %
                       {'cur_value': str(limit),
                        'cls': name,
                        'max_value': str(lmax if lmax > 0
                                         else "not restricted"),
                        'min_value': str(lmin),
                        'max_reason': reasons[name]["max"],
                        'min_reason': reasons[name]["min"]})
                raise webob_exc.HTTPBadRequest(explanation=msg)

        # 3. everything ok -> apply changes
        updates = self.db_api.quotas_set(request.context, scope, quotas)
        return {'quotas': updates}

    def delete_quotas(self, request, scope):
        self.policy.enforce(request.context, 'reset_quotas',
                            {'scope': scope})
        self.db_api.quotas_reset(request.context, scope)

    def quota_usage(self, request, scope):
        self.policy.enforce(request.context, 'reset_quotas', {'scope': scope})
        domain = self._get_domain(scope)
        if domain is None:
            raise webob_exc.HTTPForbidden("Usage request for "
                                          "domains is not allowed")

        usage = {name: {'usage': 0, 'limit': -1} for name in quota_classes}
        cur_usage = self.db_api.quota_get_usage(request.context, scope)
        for name, usg in six.iteritems(cur_usage):
            usage[name]['usage'] = usg
        limits = self.db_api.quotas_get(request.context, scope)
        if not limits:
            limits = self.db_api.quotas_get_defaults(request.context, domain)
        for name, lim in six.iteritems(limits):
            usage[name]['limit'] = lim

        return {'usage': usage}


class ResponseSerializer(wsgi.JSONResponseSerializer):
    @staticmethod
    def _prepare_body(response, values):
        body = jsonutils.dumps(values, ensure_ascii=False)
        response.unicode_body = six.text_type(body)
        response.content_type = 'application/json'

    def get_quota_classes(self, response, quota_classes):
        self._prepare_body(response, quota_classes)

    def set_quota_classes(self, response, quota_classes):
        self._prepare_body(response, quota_classes)

    def get_quotas(self, response, quotas):
        self._prepare_body(response, quotas)

    def set_quotas(self, response, quotas):
        self._prepare_body(response, quotas)

    def quota_usage(self, response, usage):
        self._prepare_body(response, usage)


_props = {
    name: {
        "type": "integer",
        "minimum": -1,
        "maximum": getattr(sys, 'maxint', sys.maxsize),
    }
    for name in quota_classes
}


def get_quota_schema():
    properties = {
        "quotas": {
            "type": "object",
            "properties": _props,
            'required': quota_classes,
        }
    }
    return glance.schema.Schema('quota', properties=properties)


def get_quota_class_schema():
    properties = {
        "quota_classes": {
            "type": "object",
            "properties": _props,
            'required': quota_classes,
        }
    }
    return glance.schema.Schema('quota_class', properties=properties)


def create_resource():
    """Quota resource factory method"""
    deserializer = RequestDeserializer()
    serializer = ResponseSerializer()
    controller = QuotaController()
    return wsgi.Resource(controller, deserializer, serializer)
