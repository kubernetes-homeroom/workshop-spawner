# This file provides configuration specific to the 'terminal-server'
# deployment mode. In this mode authentication for JupyterHub is done
# against the OpenShift cluster using OAuth.

# Work out the public server address for the OpenShift OAuth endpoint.
# Make sure the request is done in a session so the connection is closed
# and later calls against the REST API don't attempt to reuse it. This
# is just to avoid potential for any problems with connection reuse.

import json
import requests

from fnmatch import fnmatch

from tornado import web, gen

kubernetes_service_host = os.environ['KUBERNETES_SERVICE_HOST']
kubernetes_service_port = os.environ['KUBERNETES_SERVICE_PORT']

kubernetes_server_url = 'https://%s:%s' % (kubernetes_service_host,
        kubernetes_service_port)

oauth_metadata_url = '%s/.well-known/oauth-authorization-server' % kubernetes_server_url

with requests.Session() as session:
    response = session.get(oauth_metadata_url, verify=False)
    data = json.loads(response.content.decode('UTF-8'))
    oauth_issuer_address = data['issuer']

# Enable the OpenShift authenticator. The OPENSHIFT_URL environment
# variable must be set before importing the authenticator as it only
# reads it when module is first imported. From OpenShift 4.0 we need
# to supply separate URLs for Kubernetes server and OAuth server.

os.environ['OPENSHIFT_URL'] = oauth_issuer_address

os.environ['OPENSHIFT_REST_API_URL'] = kubernetes_server_url
os.environ['OPENSHIFT_AUTH_API_URL'] = oauth_issuer_address

from oauthenticator.openshift import OpenShiftOAuthenticator
c.JupyterHub.authenticator_class = OpenShiftOAuthenticator

OpenShiftOAuthenticator.scope = ['user:full']

client_id = '%s-%s-console' % (application_name, namespace)
client_secret = os.environ['OAUTH_CLIENT_SECRET']

c.OpenShiftOAuthenticator.client_id = client_id
c.OpenShiftOAuthenticator.client_secret = client_secret
c.Authenticator.enable_auth_state = True

c.CryptKeeper.keys = [ client_secret.encode('utf-8') ]

c.OpenShiftOAuthenticator.oauth_callback_url = (
        'https://%s/hub/oauth_callback' % public_hostname)

c.Authenticator.auto_login = True

# Enable admin access to designated users of the OpenShift cluster.

c.JupyterHub.admin_access = True

c.Authenticator.admin_users = set(os.environ.get('ADMIN_USERS', '').split())

# Override labels on pods so matches label used by the spawner.

c.KubeSpawner.common_labels = {
    'app': '%s-%s' % (application_name, namespace)
}

c.KubeSpawner.extra_labels = {
    'spawner': 'terminal-server',
    'class': 'session',
    'user': '{username}'
}

# Mount config map for user provided environment variables for the
# terminal and workshop.

c.KubeSpawner.volumes = [
    {
        'name': 'envvars',
        'configMap': {
            'name': '%s-env' % application_name,
            'defaultMode': 420
        }
    }
]

c.KubeSpawner.volume_mounts = [
    {
        'name': 'envvars',
        'mountPath': '/opt/workshop/envvars'
    }
]

# For workshops we provide each user with a persistent volume so they
# don't loose their work. This is mounted on /opt/app-root, so we need
# to copy the contents from the image into the persistent volume the
# first time using an init container.
#
# Note that if a profiles list is used, there must still be a default
# terminal image setup we can use to run the init container. The image
# is what contains the script which copies the file into the persistent
# volume. Perhaps should use the JupyterHub image for the init container
# and add the script which performs the copy to this image.

volume_size = os.environ.get('VOLUME_SIZE')

if volume_size:
    c.KubeSpawner.pvc_name_template = '%s-user' % c.KubeSpawner.pod_name_template

    c.KubeSpawner.storage_pvc_ensure = True

    c.KubeSpawner.storage_capacity = volume_size

    c.KubeSpawner.storage_access_modes = ['ReadWriteOnce']

    c.KubeSpawner.volumes.extend([
        {
            'name': 'data',
            'persistentVolumeClaim': {
                'claimName': c.KubeSpawner.pvc_name_template
            }
        }
    ])

    c.KubeSpawner.volume_mounts.extend([
        {
            'name': 'data',
            'mountPath': '/opt/app-root',
            'subPath': 'workspace'
        }
    ])

    c.KubeSpawner.init_containers.extend([
        {
            'name': 'setup-volume',
            'image': '%s' % c.KubeSpawner.image_spec,
            'command': [
                '/opt/workshop/bin/setup-volume.sh',
                '/opt/app-root',
                '/mnt/workspace'
            ],
            "resources": {
                "limits": {
                    "memory": os.environ.get('WORKSHOP_MEMORY', '128Mi')
                },
                "requests": {
                    "memory": os.environ.get('WORKSHOP_MEMORY', '128Mi')
                }
            },
            'volumeMounts': [
                {
                    'name': 'data',
                    'mountPath': '/mnt'
                }
            ]
        }
    ])

# Make modifications to pod based on user and type of session.

@gen.coroutine
def modify_pod_hook(spawner, pod):
    # Set the session access token from the OpenShift login in
    # both the terminal and console containers. We still mount
    # the service account token still as well because the console
    # needs the SSL certificate contained in it when accessing
    # the cluster REST API.

    auth_state = yield spawner.user.get_auth_state()

    pod.spec.containers[0].env.append(
            dict(name='OPENSHIFT_TOKEN', value=auth_state['access_token']))

    return pod

c.KubeSpawner.modify_pod_hook = modify_pod_hook

# Setup culling of terminal instances if timeout parameter is supplied.

idle_timeout = os.environ.get('IDLE_TIMEOUT')

if idle_timeout and int(idle_timeout):
    c.JupyterHub.services.extend([
        {
            'name': 'cull-idle',
            'admin': True,
            'command': ['cull-idle-servers', '--timeout=%s' % idle_timeout],
        }
    ])

# Pass through for dashboard the URL where should be redirected in order
# to restart a session, with a new instance created with fresh image.

c.Spawner.environment['RESTART_URL'] = '/restart'

# Redirect handler for sending /restart back to home page for user.

from jupyterhub.handlers import BaseHandler

class RestartRedirectHandler(BaseHandler):

    @web.authenticated
    @gen.coroutine
    def get(self, *args):
        user = self.get_current_user()
        if user.running:
            status = yield user.spawner.poll_and_notify()
            if status is None:
                yield self.stop_single_user(user)
        self.redirect('/hub/spawn')

c.JupyterHub.extra_handlers.extend([
    (r'/restart$', RestartRedirectHandler),
])
