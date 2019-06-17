#   Copyright 2013 Rackspace Hosting
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.

"""The shelved mode extension."""

from webob import exc

from nova.api.openstack import common
from nova.api.openstack import wsgi
from nova.compute import api as compute
from nova.compute import vm_states
from nova import exception
from nova.i18n import _
from nova import network
from nova.policies import shelve as shelve_policies


class ShelveController(wsgi.Controller):
    def __init__(self):
        super(ShelveController, self).__init__()
        self.compute_api = compute.API()
        self.network_api = network.API()

    @wsgi.response(202)
    @wsgi.expected_errors((404, 409))
    @wsgi.action('shelve')
    def _shelve(self, req, id, body):
        """Move an instance into shelved mode."""
        context = req.environ["nova.context"]

        instance = common.get_instance(self.compute_api, context, id)
        context.can(shelve_policies.POLICY_ROOT % 'shelve',
                    target={'user_id': instance.user_id,
                            'project_id': instance.project_id})
        try:
            self.compute_api.shelve(context, instance)
        except exception.InstanceIsLocked as e:
            raise exc.HTTPConflict(explanation=e.format_message())
        except exception.InstanceInvalidState as state_error:
            common.raise_http_conflict_for_instance_invalid_state(state_error,
                                                                  'shelve', id)

    @wsgi.response(202)
    @wsgi.expected_errors((404, 409))
    @wsgi.action('shelveOffload')
    def _shelve_offload(self, req, id, body):
        """Force removal of a shelved instance from the compute node."""
        context = req.environ["nova.context"]
        context.can(shelve_policies.POLICY_ROOT % 'shelve_offload')

        instance = common.get_instance(self.compute_api, context, id)
        try:
            self.compute_api.shelve_offload(context, instance)
        except exception.InstanceIsLocked as e:
            raise exc.HTTPConflict(explanation=e.format_message())
        except exception.InstanceInvalidState as state_error:
            common.raise_http_conflict_for_instance_invalid_state(state_error,
                                                              'shelveOffload',
                                                              id)

    @wsgi.response(202)
    @wsgi.expected_errors((400, 404, 409))
    @wsgi.action('unshelve')
    def _unshelve(self, req, id, body):
        """Restore an instance from shelved mode."""
        context = req.environ["nova.context"]
        context.can(shelve_policies.POLICY_ROOT % 'unshelve')
        instance = common.get_instance(self.compute_api, context, id)

        # We could potentially move this check to conductor and avoid the
        # extra API call to neutron when we support move operations with ports
        # having resource requests.
        if (instance.vm_state == vm_states.SHELVED_OFFLOADED
                and common.instance_has_port_with_resource_request(
                    context, instance.uuid, self.network_api)
                and not common.supports_port_resource_request_during_move(
                    req)):
            msg = _("The unshelve action on a server with ports having "
                    "resource requests, like a port with a QoS minimum "
                    "bandwidth policy, is not supported with this "
                    "microversion")
            raise exc.HTTPBadRequest(explanation=msg)

        try:
            self.compute_api.unshelve(context, instance)
        except exception.InstanceIsLocked as e:
            raise exc.HTTPConflict(explanation=e.format_message())
        except exception.InstanceInvalidState as state_error:
            common.raise_http_conflict_for_instance_invalid_state(state_error,
                                                                  'unshelve',
                                                                  id)
