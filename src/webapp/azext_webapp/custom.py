# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

from __future__ import print_function
import json
from knack.log import get_logger
from azure.mgmt.web.models import (AppServicePlan, SkuDescription)
from azure.cli.command_modules.appservice.custom import (
    create_webapp,
    show_webapp,
    update_app_settings,
    get_app_settings,
    _get_site_credential,
    _get_scm_url,
    get_sku_name,
    list_publish_profiles,
    get_site_configs,
    update_container_settings)
from azure.cli.command_modules.appservice._appservice_utils import _generic_site_operation
from .acr_util import (queue_acr_build, generate_img_name)
from .create_util import (
    zip_contents_from_dir,
    get_runtime_version_details,
    create_resource_group,
    check_resource_group_exists,
    check_resource_group_supports_os,
    check_if_asp_exists,
    check_app_exists,
    get_lang_from_content,
    web_client_factory,
    should_create_new_rg,
    should_create_new_asp,
    should_create_new_app
)
from ._constants import (NODE_RUNTIME_NAME, OS_DEFAULT, STATIC_RUNTIME_NAME, PYTHON_RUNTIME_NAME)
logger = get_logger(__name__)

# pylint:disable=no-member,too-many-lines,too-many-locals,too-many-statements,too-many-branches,line-too-long

def create_deploy_container_app(cmd, name, source_location=None, docker_custom_image_name=None, dryrun=False, registry_rg=None, registry_name=None):  # pylint: disable=too-many-statements
    import os
    if not source_location:
        # the dockerfile is expected to be in the current directory the command is running from
        source_location = os.getcwd()

    client = web_client_factory(cmd.cli_ctx)
    _create_new_rg = True
    _create_new_asp = True
    _create_new_app = True
    _create_acr_img = True

    if docker_custom_image_name:
        img_name = docker_custom_image_name
        _create_acr_img = False
    else:
        img_name = generate_img_name(source_location)
        logger.warning("Starting ACR build")
        queue_acr_build(cmd, registry_rg, registry_name, img_name, source_location)
        logger.warning("ACR build done. Deploying web app.")

    sku = 'P1V2'
    full_sku = get_sku_name(sku)
    location = 'Central US'
    loc_name = 'centralus'
    asp = "appsvc_asp_linux_{}".format(loc_name)
    rg_name = "appsvc_rg_linux_{}".format(loc_name)
    # Resource group: check if default RG is set
    default_rg = cmd.cli_ctx.config.get('defaults', 'group', fallback=None)
    _create_new_rg = should_create_new_rg(cmd, default_rg, rg_name, True)

    rg_str = "{}".format(rg_name)

    dry_run_str = r""" {
            "name" : "%s",
            "serverfarm" : "%s",
            "resourcegroup" : "%s",
            "sku": "%s",
            "location" : "%s"
            }
            """ % (name, asp, rg_str, full_sku, location)
            #, docker_custom_image_name)
    create_json = json.loads(dry_run_str)

    if dryrun:
        logger.warning("Web app will be created with the below configuration,re-run command "
                       "without the --dryrun flag to create & deploy a new app")
        return create_json

    # create RG if the RG doesn't already exist
    if _create_new_rg:
        logger.warning("Creating Resource group '%s' ...", rg_name)
        create_resource_group(cmd, rg_name, location)
        logger.warning("Resource group creation complete")
        _create_new_asp = True
    else:
        logger.warning("Resource group '%s' already exists.", rg_name)
        _create_new_asp = should_create_new_asp(cmd, rg_name, asp, location)
    # create new ASP if an existing one cannot be used
    if _create_new_asp:
        logger.warning("Creating App service plan '%s' ...", asp)
        sku_def = SkuDescription(tier=full_sku, name=sku, capacity=1)
        plan_def = AppServicePlan(location=loc_name, app_service_plan_name=asp,
                                  sku=sku_def, reserved=True)
        client.app_service_plans.create_or_update(rg_name, asp, plan_def)
        logger.warning("App service plan creation complete")
        _create_new_app = True
    else:
        logger.warning("App service plan '%s' already exists.", asp)
        _create_new_app = should_create_new_app(cmd, rg_name, name)
    
    # create the app
    if _create_new_app:
        logger.warning("Creating app '%s' ....", name)
        # TODO: Deploy without container params and update separately instead?
        # deployment_container_image_name=docker_custom_image_name)
        create_webapp(cmd, rg_name, name, asp, deployment_container_image_name=img_name) 
        logger.warning("Webapp creation complete")
        _set_build_appSetting = True
    else:
        logger.warning("App '%s' already exists", name)

    # Set up the container
    if _create_acr_img:
        logger.warning("Configuring ACR container settings.")
        registry_url = 'https://' + registry_name + '.azurecr.io'
        acr_img_name = registry_name + '.azurecr.io/' + img_name
        update_container_settings(cmd, rg_name, name, registry_url, acr_img_name)

    logger.warning("All done.")
    return create_json

def _ping_scm_site(cmd, resource_group, name):
    #  wakeup kudu, by making an SCM call
    import requests
    #  work around until the timeout limits issue for linux is investigated & fixed
    user_name, password = _get_site_credential(cmd.cli_ctx, resource_group, name)
    scm_url = _get_scm_url(cmd, resource_group, name)
    import urllib3
    authorization = urllib3.util.make_headers(basic_auth='{}:{}'.format(user_name, password))
    requests.get(scm_url + '/api/settings', headers=authorization)


def _get_app_url(cmd, rg_name, app_name):
    site = _generic_site_operation(cmd.cli_ctx, rg_name, app_name, 'get')
    return "https://" + site.enabled_host_names[0]


def _check_for_ready_tunnel(remote_debugging, tunnel_server):
    default_port = tunnel_server.is_port_set_to_default()
    if default_port is not remote_debugging:
        return True
    return False


def create_tunnel(cmd, resource_group_name, name, port=None, slot=None):
    logger.warning("remote-connection is deprecated and moving to cli-core, use `webapp create-remote-connection`")

    webapp = show_webapp(cmd, resource_group_name, name, slot)
    is_linux = webapp.reserved
    if not is_linux:
        logger.error("Only Linux App Service Plans supported, Found a Windows App Service Plan")
        return
    import time
    profiles = list_publish_profiles(cmd, resource_group_name, name, slot)
    user_name = next(p['userName'] for p in profiles)
    user_password = next(p['userPWD'] for p in profiles)
    import threading
    from .tunnel import TunnelServer

    if port is None:
        port = 0  # Will auto-select a free port from 1024-65535
        logger.info('No port defined, creating on random free port')
    host_name = name
    if slot is not None:
        host_name += "-" + slot
    tunnel_server = TunnelServer('', port, host_name, user_name, user_password)
    config = get_site_configs(cmd, resource_group_name, name, slot)
    _ping_scm_site(cmd, resource_group_name, name)

    t = threading.Thread(target=_start_tunnel, args=(tunnel_server, config.remote_debugging_enabled))
    t.daemon = True
    t.start()

    # Wait indefinitely for CTRL-C
    while True:
        time.sleep(5)


def _start_tunnel(tunnel_server, remote_debugging_enabled):
    import time
    if not _check_for_ready_tunnel(remote_debugging_enabled, tunnel_server):
        logger.warning('Tunnel is not ready yet, please wait (may take up to 1 minute)')
        while True:
            time.sleep(1)
            logger.warning('.')
            if _check_for_ready_tunnel(remote_debugging_enabled, tunnel_server):
                break
    if remote_debugging_enabled is False:
        logger.warning('SSH is available { username: root, password: Docker! }')
    tunnel_server.start_server()


# zip deployment copies from core with some error handling enabled, once we get the fix in core we will remove this
def enable_zip_deploy(cmd, resource_group_name, name, src, slot=None):
    user_name, password = _get_site_credential(cmd.cli_ctx, resource_group_name, name, slot)
    scm_url = _get_scm_url(cmd, resource_group_name, name, slot)
    zip_url = scm_url + '/api/zipdeploy?isAsync=true'
    deployment_status_url = scm_url + '/api/deployments/latest'

    import urllib3
    authorization = urllib3.util.make_headers(basic_auth='{0}:{1}'.format(user_name, password))
    headers = authorization
    headers['content-type'] = 'application/octet-stream'

    import requests
    import os
    # Read file content
    with open(os.path.realpath(os.path.expanduser(src)), 'rb') as fs:
        zip_content = fs.read()
        requests.post(zip_url, data=zip_content, headers=headers)
    # check the status of async deployment
    try:
        from json.decoder import JSONDecodeError
    except ImportError:
        JSONDecodeError = ValueError

    response = requests.get(deployment_status_url, headers=authorization)
    try:
        response = response.json()
        if response.get('status', 0) != 4:
            logger.warning(response.get('progress', ''))
            response = _check_zip_deployment_status(deployment_status_url, authorization)
        return response
    except JSONDecodeError:
        logger.warning("""Unable to fetch status of deployment. Please check status manually using link '%s'
            """, deployment_status_url)


def _check_zip_deployment_status(deployment_status_url, authorization):
    import requests
    import time
    num_trials = 1
    while num_trials < 10:
        time.sleep(15)
        response = requests.get(deployment_status_url, headers=authorization)
        res_dict = response.json()
        num_trials = num_trials + 1
        if res_dict.get('status', 0) == 5:
            logger.warning("Zip deployment failed status %s", res_dict['status_text'])
            break
        elif res_dict.get('status', 0) == 4:
            break
        if 'progress' in res_dict:
            logger.info(res_dict['progress'])  # show only in debug mode, customers seem to find this confusing
    # if the deployment is taking longer than expected
    if res_dict.get('status', 0) != 4:
        logger.warning("""Deployment is taking longer than expected. Please verify status at '%s'
            beforing launching the app""", deployment_status_url)
    return res_dict
