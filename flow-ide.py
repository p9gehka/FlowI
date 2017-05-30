from collections import namedtuple
import json
import os
import sublime
import sublime_plugin
import subprocess

CLIRequirements = namedtuple('CLIRequirements', [
    'filename', 'project_root', 'contents', 'cursor_pos', 'row', 'col'
])

settings = None
plugin_ready = False


def plugin_loaded():
    global settings
    global plugin_ready
    settings = sublime.load_settings('FlowIDE.sublime-settings')
    plugin_ready = True


def wait_for_load(func):
    def wrapper(*args, **kwargs):
        global plugin_ready
        if not plugin_ready:
            return
        return func(*args, **kwargs)

    return wrapper


def find_flow_config(filename):
    if not filename:
        return '/'

    if filename is '/':
        return '/'

    potential_root = os.path.dirname(filename)
    if os.path.isfile(os.path.join(potential_root, '.flowconfig')):
        print('FlowIDE: using .flowconfig at', potential_root)
        return potential_root

    return find_flow_config(potential_root)


def find_flow_bin(root_dir):
    if settings.get('use_npm_flow'):
        npm_flow_bin = os.path.join(
            root_dir, 'node_modules/.bin/flow'
        )
        if os.path.isfile(npm_flow_bin):
            print('FlowIDE: using npm flow binary at', npm_flow_bin)
            return npm_flow_bin

    flow_path = settings.get('flow_path', 'flow')
    print('FlowIDE: using binary at', flow_path)
    return flow_path


def build_snippet(name, params):
    snippet = name + '({})'
    paramText = ''

    for param in params:
        if not paramText:
            paramText += param['name']
        else:
            paramText += ', ' + param['name']

    return snippet.format(paramText)


def rowcol_to_region(view, row, col, endcol):
    start = view.text_point(row, col)
    end = view.text_point(row, endcol)
    return sublime.Region(start, end)


def parse_cli_dependencies(view, **kwargs):
    filename = view.file_name()
    project_root = find_flow_config(filename)

    cursor_pos = view.sel()[0].begin()
    row, col = view.rowcol(cursor_pos)

    current_contents = view.substr(
        sublime.Region(0, view.size())
    )

    if kwargs.get('add_magic_token'):
        current_lines = current_contents.splitlines()
        current_line = current_lines[row]
        tokenized_line = current_line[0:col] + 'AUTO332' + current_line[col:-1]
        current_lines[row] = tokenized_line
        current_contents = '\n'.join(current_lines)

    return CLIRequirements(
        filename=filename,
        project_root=project_root,
        contents=current_contents,
        cursor_pos=cursor_pos,
        row=row, col=col
    )


def call_flow_cli(contents, command):
    print(command)
    # Use a pipe for flow autocomplete's stdin
    read, write = os.pipe()
    os.write(write, str.encode(contents))
    os.close(write)

    try:
        output = subprocess.check_output(
            command, stderr=subprocess.STDOUT, stdin=read
        )
        result = json.loads(output.decode('utf-8'))
        os.close(read)
        return result
    except subprocess.CalledProcessError as e:
        try:
            result = json.loads(e.output.decode('utf-8'))
            os.close(read)
            return result
        except:
            os.close(read)
            print(e.output)
            return None


class FlowGoToDefinition(sublime_plugin.TextCommand):
    @wait_for_load
    def run(self, edit):
        sublime.set_timeout_async(self.run_async)

    def run_async(self):
        deps = parse_cli_dependencies(self.view)
        if deps.project_root is '/':
            return

        flow = find_flow_bin(deps.project_root)

        result = call_flow_cli(deps.contents, [
            flow, 'get-def',
            '--from', 'nuclide',
            '--root', deps.project_root,
            '--path', deps.filename,
            '--json',
            str(deps.row + 1), str(deps.col + 1)
        ])

        if result and result['path']:
            sublime.active_window().open_file(
                result['path'] +
                ':' + str(result['line']) +
                ':' + str(result['start']),
                sublime.ENCODED_POSITION |
                sublime.TRANSIENT
            )


class FlowTypeHint(sublime_plugin.TextCommand):
    @wait_for_load
    def run(self, edit):
        sublime.set_timeout_async(self.run_async)

    def run_async(self):
        deps = parse_cli_dependencies(self.view)
        if deps.project_root is '/':
            return

        flow = find_flow_bin(deps.project_root)

        result = call_flow_cli(deps.contents, [
            flow, 'type-at-pos',
            '--from', 'nuclide',
            '--root', deps.project_root,
            '--path', deps.filename,
            '--json',
            str(deps.row + 1), str(deps.col + 1)
        ])
        if result:
            self.view.show_popup(result['type'].replace('<', '&lt;').replace('>', '&gt;'))


class FlowListener(sublime_plugin.EventListener):
    completions = None
    completions_ready = False

    # Used for async completions.
    def run_auto_complete(self):
        sublime.active_window().active_view().run_command("auto_complete", {
            'disable_auto_insert': True,
            'api_completions_only': False,
            'next_completion_if_showing': False,
            'auto_complete_commit_on_tab': True,
        })

    @wait_for_load
    def on_query_completions(self, view, prefix, locations):
        # Return the pending completions and clear them
        if self.completions_ready and self.completions:
            self.completions_ready = False
            return self.completions

        sublime.set_timeout_async(
            lambda: self.on_query_completions_async(
                view, prefix, locations
            )
        )

    def on_query_completions_async(self, view, prefix, locations):
        self.completions = None

        if not view.match_selector(
            locations[0],
            'source.js - string - comment'
        ):
            return

        deps = parse_cli_dependencies(view, add_magic_token=True)
        if deps.project_root is '/':
            return

        flow = find_flow_bin(deps.project_root)

        result = call_flow_cli(deps.contents, [
            flow, 'autocomplete',
            '--from', 'nuclide',
            '--retry-if-init', 'false',
            '--root', deps.project_root,
            '--json',
            deps.filename,
        ])

        if result:
            self.completions = (
                [
                    (
                        match['name'] + '\t' + match['type'],
                        build_snippet(
                            match['name'],
                            match.get('func_details')['params']
                        )
                        if match.get('func_details') else match['name']
                    )
                    for match in result['result']
                ],
                sublime.INHIBIT_WORD_COMPLETIONS |
                sublime.INHIBIT_EXPLICIT_COMPLETIONS
            )
            self.completions_ready = True
            sublime.active_window().active_view().run_command(
                'hide_auto_complete'
            )
            self.run_auto_complete()

    @wait_for_load
    def on_selection_modified_async(self, view):
        deps = parse_cli_dependencies(view)
        if deps.project_root is '/':
            return

        if (
            '// @flow' not in deps.contents and
            '/* @flow */' not in deps.contents
        ):
            return view.erase_regions('flow_error')

        scope = view.scope_name(deps.cursor_pos)
        if 'source.js' not in scope:
            return

        flow = find_flow_bin(deps.project_root)

        result = call_flow_cli(deps.contents, [
            flow, 'check-contents',
            '--from', 'nuclide',
            '--json',
            deps.filename
        ])

        if result:
            if result['passed']:
                view.erase_regions('flow_error')
                view.set_status('flow_error', 'Flow: no errors')
                return

            regions = []
            description_by_row = {}

            for error in result['errors']:
                rows = []
                description = ''

                operation = error.get('operation')
                if operation:
                    row = int(operation['line']) - 1
                    col = int(operation['start']) - 1
                    endcol = int(operation['end'])
                    regions.append(rowcol_to_region(view, row, col, endcol))
                    rows.append(row)

                for message in error['message']:
                    row = int(message['line']) - 1
                    col = int(message['start']) - 1
                    endcol = int(message['end'])
                    regions.append(rowcol_to_region(view, row, col, endcol))
                    rows.append(row)

                    description += message['descr'] + ' '

                for row in rows:
                    row_description = description_by_row.get(row)
                    if not row_description:
                        description_by_row[row] = description
                    if row_description and description not in row_description:
                        description_by_row[row] += '; ' + description

            view.add_regions(
                'flow_error', regions, 'scope.js', 'dot',
                sublime.DRAW_NO_FILL
            )

            error_count = len(result['errors'])
            error_count_text = 'Flow: {} error{}'.format(
                error_count, '' if error_count is 1 else 's'
            )
            error_for_row = description_by_row.get(deps.row)
            if error_for_row:
                view.set_status(
                    'flow_error', error_count_text + ': ' + error_for_row
                )
            else:
                view.set_status('flow_error', error_count_text)
