# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

from azure.cli.core import AzCommandsLoader

import azext_webapps._help  # pylint: disable=unused-import


class WebappsCommandLoader(AzCommandsLoader):

    def __init__(self, cli_ctx=None):
        from azure.cli.core.commands import CliCommandType
        webapps_custom = CliCommandType(
            operations_tmpl='azext_webapps.custom#{}')
        super(WebappsCommandLoader, self).__init__(cli_ctx=cli_ctx,
                                                   custom_command_type=webapps_custom)

    def load_command_table(self, _):
        with self.command_group('webapp') as g:
            g.custom_command('quickstart', 'create_deploy_webapp')

        return self.command_table

    def load_arguments(self, _):
        with self.argument_context('webapp quickstart') as c:
            c.argument('name', options_list=['--name', '-n'], help='name of the new webapp')
            c.argument('dryrun',
                       help="shows summary of the create operation instead of actually creating and deploying the app",
                       default=False)

COMMAND_LOADER_CLS = WebappsCommandLoader
