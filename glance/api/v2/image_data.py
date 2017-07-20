# Copyright 2012 OpenStack Foundation.
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
from cursive import exception as cursive_exception
import glance_store
from glance_store import backend
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import encodeutils
from oslo_utils import excutils
import six
import webob.exc

import glance.api.policy
from glance.common import exception
from glance.common import quota
from glance.common import store_utils
from glance.common import trust_auth
from glance.common import utils
from glance.common import wsgi
import glance.db
import glance.gateway
from glance.i18n import _, _LE, _LI
import glance.notifier


LOG = logging.getLogger(__name__)

CONF = cfg.CONF


class ImageDataController(object):
    def __init__(self, db_api=None, store_api=None,
                 policy_enforcer=None, notifier=None,
                 gateway=None):
        if gateway is None:
            db_api = db_api or glance.db.get_api()
            store_api = store_api or glance_store
            policy = policy_enforcer or glance.api.policy.Enforcer()
            notifier = notifier or glance.notifier.Notifier()
            gateway = glance.gateway.Gateway(db_api, store_api,
                                             notifier, policy)
        self.gateway = gateway

    def _restore(self, image_repo, image):
        """
        Restore the image to queued status.

        :param image_repo: The instance of ImageRepo
        :param image: The image will be restored
        """
        try:
            if image_repo and image:
                image.status = 'queued'
                image_repo.save(image)
        except Exception as e:
            msg = (_LE("Unable to restore image %(image_id)s: %(e)s") %
                   {'image_id': image.image_id,
                    'e': encodeutils.exception_to_unicode(e)})
            LOG.exception(msg)

    def _unstage(self, image_repo, image, staging_store):
        """
        Restore the image to queued status and remove data from staging.

        :param image_repo: The instance of ImageRepo
        :param image: The image will be restored
        :param staging_store: The store used for staging
        """
        loc = glance_store.location.get_location_from_uri(str(
            CONF.node_staging_uri + '/' + image.image_id))
        try:
            staging_store.delete(loc)
        except glance_store.exceptions.NotFound:
            pass
        finally:
            self._restore(image_repo, image)

    def _delete(self, image_repo, image):
        """Delete the image.

        :param image_repo: The instance of ImageRepo
        :param image: The image that will be deleted
        """
        try:
            if image_repo and image:
                image.status = 'killed'
                image_repo.save(image)
        except Exception as e:
            msg = (_LE("Unable to delete image %(image_id)s: %(e)s") %
                   {'image_id': image.image_id,
                    'e': encodeutils.exception_to_unicode(e)})
            LOG.exception(msg)

    @utils.mutating
    def upload(self, req, image_id, data, size):
        image_repo = self.gateway.get_repo(req.context)
        image = None
        refresher = None
        cxt = req.context
        try:
            image = image_repo.get(image_id)
            image.status = 'saving'
            try:
                if CONF.data_api == 'glance.db.registry.api':
                    # create a trust if backend is registry
                    try:
                        # request user plugin for current token
                        user_plugin = req.environ.get('keystone.token_auth')
                        roles = []
                        # use roles from request environment because they
                        # are not transformed to lower-case unlike cxt.roles
                        for role_info in req.environ.get(
                                'keystone.token_info')['token']['roles']:
                            roles.append(role_info['name'])
                        refresher = trust_auth.TokenRefresher(user_plugin,
                                                              cxt.tenant,
                                                              roles)
                    except Exception as e:
                        LOG.info(_LI("Unable to create trust: %s "
                                     "Use the existing user token."),
                                 encodeutils.exception_to_unicode(e))

                image_repo.save(image, from_state='queued')
                image.set_data(data, size)
                meta = quota.convert_proxy_to_metadata(image)
                with quota.QuotaDriver.activate(req.context, image_id, meta):
                    try:
                        image_repo.save(image, from_state='saving')
                    except exception.NotAuthenticated:
                        if refresher is not None:
                            # request a new token to update an image in DB
                            cxt.auth_token = refresher.refresh_token()
                            image_repo = self.gateway.get_repo(req.context)
                            image_repo.save(image, from_state='saving')
                        else:
                            raise

                    try:
                        # release resources required for re-auth
                        if refresher is not None:
                            refresher.release_resources()
                    except Exception as e:
                        LOG.info(_LI("Unable to delete trust %(trust)s: "
                                     "%(msg)s"),
                                 {"trust": refresher.trust_id,
                                  "msg": encodeutils.exception_to_unicode(e)})
            except (glance_store.NotFound,
                    exception.ImageNotFound,
                    exception.Conflict):
                msg = (_("Image %s could not be found after upload. "
                         "The image may have been deleted during the "
                         "upload, cleaning up the chunks uploaded.") %
                       image_id)
                LOG.warn(msg)
                # NOTE(sridevi): Cleaning up the uploaded chunks.
                try:
                    image.delete()
                except exception.ImageNotFound:
                    # NOTE(sridevi): Ignore this exception
                    pass
                raise webob.exc.HTTPGone(explanation=msg,
                                         request=req,
                                         content_type='text/plain')
            except exception.NotAuthenticated:
                msg = (_("Authentication error - the token may have "
                         "expired during file upload. Deleting image data for "
                         "%s.") % image_id)
                LOG.debug(msg)
                try:
                    image.delete()
                except exception.NotAuthenticated:
                    # NOTE: Ignore this exception
                    pass
                raise webob.exc.HTTPUnauthorized(explanation=msg,
                                                 request=req,
                                                 content_type='text/plain')
        except exception.ProjectQuotaFull as e:
                LOG.info(_LI('Cleaning up %s after exceeding the quota.'),
                         image.image_id)
                store_utils.safe_delete_from_backend(
                    req.context, image.image_id, image.locations[0])
                image = image_repo.get(image_id)
                self._restore(image_repo, image)
                raise webob.exc.HTTPForbidden(explanation=e,
                                              request=req,
                                              content_type='text/plain')
        except ValueError as e:
            LOG.debug("Cannot save data for image %(id)s: %(e)s",
                      {'id': image_id,
                       'e': encodeutils.exception_to_unicode(e)})
            self._restore(image_repo, image)
            raise webob.exc.HTTPBadRequest(
                explanation=encodeutils.exception_to_unicode(e))
        except glance_store.StoreAddDisabled:
            msg = _("Error in store configuration. Adding images to store "
                    "is disabled.")
            LOG.exception(msg)
            self._restore(image_repo, image)
            raise webob.exc.HTTPGone(explanation=msg, request=req,
                                     content_type='text/plain')

        except exception.InvalidImageStatusTransition as e:
            msg = encodeutils.exception_to_unicode(e)
            LOG.exception(msg)
            raise webob.exc.HTTPConflict(explanation=e.msg, request=req)

        except exception.Forbidden as e:
            msg = ("Not allowed to upload image data for image %s" %
                   image_id)
            LOG.debug(msg)
            raise webob.exc.HTTPForbidden(explanation=msg, request=req)

        except exception.NotFound as e:
            raise webob.exc.HTTPNotFound(explanation=e.msg)

        except glance_store.StorageFull as e:
            msg = _("Image storage media "
                    "is full: %s") % encodeutils.exception_to_unicode(e)
            LOG.error(msg)
            self._restore(image_repo, image)
            raise webob.exc.HTTPRequestEntityTooLarge(explanation=msg,
                                                      request=req)

        except exception.StorageQuotaFull as e:
            msg = _("Image exceeds the storage "
                    "quota: %s") % encodeutils.exception_to_unicode(e)
            LOG.error(msg)
            self._restore(image_repo, image)
            raise webob.exc.HTTPRequestEntityTooLarge(explanation=msg,
                                                      request=req)

        except exception.ImageSizeLimitExceeded as e:
            msg = _("The incoming image is "
                    "too large: %s") % encodeutils.exception_to_unicode(e)
            LOG.error(msg)
            self._restore(image_repo, image)
            raise webob.exc.HTTPRequestEntityTooLarge(explanation=msg,
                                                      request=req)

        except glance_store.StorageWriteDenied as e:
            msg = _("Insufficient permissions on image "
                    "storage media: %s") % encodeutils.exception_to_unicode(e)
            LOG.error(msg)
            self._restore(image_repo, image)
            raise webob.exc.HTTPServiceUnavailable(explanation=msg,
                                                   request=req)

        except cursive_exception.SignatureVerificationError as e:
            msg = (_LE("Signature verification failed for image %(id)s: %(e)s")
                   % {'id': image_id,
                      'e': encodeutils.exception_to_unicode(e)})
            LOG.error(msg)
            self._delete(image_repo, image)
            raise webob.exc.HTTPBadRequest(explanation=msg)

        except webob.exc.HTTPGone as e:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE("Failed to upload image data due to HTTP error"))

        except webob.exc.HTTPError as e:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE("Failed to upload image data due to HTTP error"))
                self._restore(image_repo, image)

        except Exception as e:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE("Failed to upload image data due to "
                              "internal error"))
                self._restore(image_repo, image)

    @utils.mutating
    def stage(self, req, image_id, data, size):
        image_repo = self.gateway.get_repo(req.context)
        image = None

        # NOTE(jokke): this is horrible way to do it but as long as
        # glance_store is in a shape it is, the only way. Don't hold me
        # accountable for it.
        def _build_staging_store(self):
            conf = cfg.ConfigOpts()
            backend.register_opts(conf)
            conf.set_override('filesystem_store_datadir',
                              CONF.node_staging_uri[7:],
                              group='glance_store')
            staging_store = backend._load_store(conf, 'file')

            try:
                staging_store.configure()
            except AttributeError:
                msg = _("'node_staging_uri' is not set correctly. Could not "
                        "load staging store.")
                raise exception.BadStoreUri(message=msg)
            return staging_store

        staging_store = _build_staging_store()

        try:
            image = image_repo.get(image_id)
            image.status = 'uploading'
            image_repo.save(image, from_state='queued')
            try:
                staging_store.add(image_id, data, 0)
            except glance_store.Duplicate as e:
                msg = _("The image %s has data on staging") % image_id
                raise webob.exc.HTTPConflict(explanation=msg)
                self._restore(image_repo, image)

        except glance_store.StorageFull as e:
            msg = _("Image storage media "
                    "is full: %s") % encodeutils.exception_to_unicode(e)
            LOG.error(msg)
            self._unstage(image_repo, image, staging_store)
            raise webob.exc.HTTPRequestEntityTooLarge(explanation=msg,
                                                      request=req)

        except exception.StorageQuotaFull as e:
            msg = _("Image exceeds the storage "
                    "quota: %s") % encodeutils.exception_to_unicode(e)
            LOG.debug(msg)
            self._unstage(image_repo, image, staging_store)
            raise webob.exc.HTTPRequestEntityTooLarge(explanation=msg,
                                                      request=req)

        except exception.ImageSizeLimitExceeded as e:
            msg = _("The incoming image is "
                    "too large: %s") % encodeutils.exception_to_unicode(e)
            LOG.debug(msg)
            self._unstage(image_repo, image, staging_store)
            raise webob.exc.HTTPRequestEntityTooLarge(explanation=msg,
                                                      request=req)

        except glance_store.StorageWriteDenied as e:
            msg = _("Insufficient permissions on image "
                    "storage media: %s") % encodeutils.exception_to_unicode(e)
            LOG.error(msg)
            self._unstage(image_repo, image)
            raise webob.exc.HTTPServiceUnavailable(explanation=msg,
                                                   request=req)

        except Exception as e:
            with excutils.save_and_reraise_exception():
                LOG.exception(_LE("Failed to stage image data due to "
                                  "internal error"))
                self._restore(image_repo, image)

    def download(self, req, image_id):
        image_repo = self.gateway.get_repo(req.context)
        try:
            image = image_repo.get(image_id)
            if image.status == 'deactivated' and not req.context.is_admin:
                msg = _('The requested image has been deactivated. '
                        'Image data download is forbidden.')
                raise exception.Forbidden(message=msg)
        except exception.NotFound as e:
            raise webob.exc.HTTPNotFound(explanation=e.msg)
        except exception.Forbidden as e:
            LOG.debug("User not permitted to download image '%s'", image_id)
            raise webob.exc.HTTPForbidden(explanation=e.msg)

        return image


class RequestDeserializer(wsgi.JSONRequestDeserializer):

    def upload(self, request):
        try:
            request.get_content_type(('application/octet-stream',))
        except exception.InvalidContentType as e:
            raise webob.exc.HTTPUnsupportedMediaType(explanation=e.msg)

        if self.is_valid_encoding(request) and self.is_valid_method(request):
            request.is_body_readable = True

        image_size = request.content_length or None
        return {'size': image_size, 'data': request.body_file}


class ResponseSerializer(wsgi.JSONResponseSerializer):

    def download(self, response, image):

        offset, chunk_size = 0, None
        # NOTE(dharinic): In case of a malformed range header,
        # glance/common/wsgi.py will raise HTTPRequestRangeNotSatisfiable
        # (setting status_code to 416)
        range_val = response.request.get_range_from_request(image.size)

        if range_val:
            if isinstance(range_val, webob.byterange.Range):
                response_end = image.size - 1
                # NOTE(dharinic): webob parsing is zero-indexed.
                # i.e.,to download first 5 bytes of a 10 byte image,
                # request should be "bytes=0-4" and the response would be
                # "bytes 0-4/10".
                # Range if validated, will never have 'start' object as None.
                if range_val.start >= 0:
                    offset = range_val.start
                else:
                    # NOTE(dharinic): Negative start values needs to be
                    # processed to allow suffix-length for Range request
                    # like "bytes=-2" as per rfc7233.
                    if abs(range_val.start) < image.size:
                        offset = image.size + range_val.start

                if range_val.end is not None and range_val.end < image.size:
                    chunk_size = range_val.end - offset
                    response_end = range_val.end - 1
                else:
                    chunk_size = image.size - offset

            # NOTE(dharinic): For backward compatibility reasons, we maintain
            # support for 'Content-Range' in requests even though it's not
            # correct to use it in requests.
            elif isinstance(range_val, webob.byterange.ContentRange):
                response_end = range_val.stop - 1
                # NOTE(flaper87): if not present, both, start
                # and stop, will be None.
                offset = range_val.start
                chunk_size = range_val.stop - offset

            response.status_int = 206

        response.headers['Content-Type'] = 'application/octet-stream'

        try:
            # NOTE(markwash): filesystem store (and maybe others?) cause a
            # problem with the caching middleware if they are not wrapped in
            # an iterator very strange
            response.app_iter = iter(image.get_data(offset=offset,
                                                    chunk_size=chunk_size))
            # NOTE(dharinic): In case of a full image download, when
            # chunk_size was none, reset it to image.size to set the
            # response header's Content-Length.
            if chunk_size is not None:
                response.headers['Content-Range'] = 'bytes %s-%s/%s'\
                                                    % (offset,
                                                       response_end,
                                                       image.size)
            else:
                chunk_size = image.size
        except glance_store.NotFound as e:
            raise webob.exc.HTTPNoContent(explanation=e.msg)
        except glance_store.RemoteServiceUnavailable as e:
            raise webob.exc.HTTPServiceUnavailable(explanation=e.msg)
        except (glance_store.StoreGetNotSupported,
                glance_store.StoreRandomGetNotSupported) as e:
            raise webob.exc.HTTPBadRequest(explanation=e.msg)
        except exception.Forbidden as e:
            LOG.debug("User not permitted to download image '%s'", image)
            raise webob.exc.HTTPForbidden(explanation=e.msg)
        # NOTE(saschpe): "response.app_iter = ..." currently resets Content-MD5
        # (https://github.com/Pylons/webob/issues/86), so it should be set
        # afterwards for the time being.
        if image.checksum:
            response.headers['Content-MD5'] = image.checksum
        # NOTE(markwash): "response.app_iter = ..." also erroneously resets the
        # content-length
        response.headers['Content-Length'] = six.text_type(chunk_size)

    def upload(self, response, result):
        response.status_int = 204


def create_resource():
    """Image data resource factory method"""
    deserializer = RequestDeserializer()
    serializer = ResponseSerializer()
    controller = ImageDataController()
    return wsgi.Resource(controller, deserializer, serializer)
