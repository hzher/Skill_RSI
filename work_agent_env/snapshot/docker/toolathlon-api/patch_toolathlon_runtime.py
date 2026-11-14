"""Apply task MCP runtime fixes; leave task evaluators unchanged."""
import argparse
from pathlib import Path


def replace(path, old, new):
    source = path.read_text()
    if source.count(new) == 1:
        return
    if source.count(old) != 1:
        raise RuntimeError(f"Unexpected source at {path}: expected one occurrence")
    path.write_text(source.replace(old, new))


def patch(root):
    forms = root / 'google-forms-mcp/src/pg-forms.ts'
    replace(forms, "required: boolean = false) {",
            "required: boolean = false, questionType: string = 'textQuestion') {\n"
            "  if (!['textQuestion', 'TEXT'].includes(questionType)) throw new Error('Invalid questionType');")
    replace(forms, "'textQuestion', required, '{}'", "questionType, required, '{}'")
    replace(forms, "  options: string[],\n  required: boolean = false\n) {",
            "  options: string[],\n  required: boolean = false,\n  questionType: string = 'choiceQuestion'\n) {\n"
            "  if (!['choiceQuestion', 'RADIO', 'CHECKBOX'].includes(questionType)) throw new Error('Invalid questionType');")
    replace(forms, "    type: 'RADIO',", "    type: questionType === 'choiceQuestion' ? 'RADIO' : questionType,")
    replace(forms, "'choiceQuestion', required, JSON.stringify(config)",
            "questionType, required, JSON.stringify(config)")
    replace(forms, "if (q.question_type === 'textQuestion') {",
            "if (q.question_type === 'textQuestion' || q.question_type === 'TEXT') {")
    replace(forms, "} else if (q.question_type === 'choiceQuestion') {",
            "} else if (['choiceQuestion', 'RADIO', 'CHECKBOX'].includes(q.question_type)) {")
    index = root / 'google-forms-mcp/src/index.ts'
    replace(index, "description: 'Question title',\n              },\n              required:",
            "description: 'Question title',\n              },\n"
            "              questionType: { type: 'string', enum: ['textQuestion', 'TEXT'], "
            "description: 'Storage type; legacy textQuestion by default, or explicit TEXT' },\n"
            "              required:")
    replace(index, "description: 'Add a multiple choice question to the form'",
            "description: 'Add a radio (single choice) or checkbox (multiple selections) question to the form'")
    replace(index, "description: 'Array of choices',\n              },\n              required:",
            "description: 'Array of choices',\n              },\n"
            "              questionType: { type: 'string', enum: ['choiceQuestion', 'RADIO', 'CHECKBOX'], "
            "description: 'Legacy choiceQuestion by default; explicit RADIO for one answer or CHECKBOX for multiple selections' },\n"
            "              required:")
    replace(index, "args.formId, args.questionTitle, args.options, args.required || false",
            "args.formId, args.questionTitle, args.options, args.required || false, args.questionType || 'choiceQuestion'")
    replace(index, "args.formId, args.questionTitle, args.required || false",
            "args.formId, args.questionTitle, args.required || false, args.questionType || 'textQuestion'")
    cli = root / 'cli-mcp-server/src/cli_mcp_server/server.py'
    anchor = '    def validate_command(self, command_string: str) -> tuple[str, List[str]]:'
    scanner = '''    @staticmethod
    def _shell_parts(command_string):
        """Split only unquoted, unescaped shell operators; preserve argument quotes."""
        operators = ("&&", "||", ">>", "<<", "|", ">", "<", ";")
        parts, start, index, quote, escaped = [], 0, 0, None, False
        while index < len(command_string):
            char = command_string[index]
            if escaped:
                escaped = False
                index += 1
                continue
            if char == chr(92) and quote != "'":
                escaped = True
                index += 1
                continue
            if quote:
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in ("'", chr(34)):
                quote = char
                index += 1
                continue
            operator = next((op for op in operators if command_string.startswith(op, index)), None)
            if operator:
                prefix = command_string[start:index].strip()
                if prefix:
                    parts.append(prefix)
                parts.append(operator)
                index += len(operator)
                start = index
            else:
                index += 1
        suffix = command_string[start:].strip()
        if suffix:
            parts.append(suffix)
        return parts

'''
    replace(cli, anchor, scanner + anchor)
    replace(cli, 'contains_shell_operator = any(\n            operator in command_string for operator in shell_operators\n        )',
            'contains_shell_operator = any(\n            part in shell_operators for part in self._shell_parts(command_string)\n        )')
    replace(cli, 'use_shell = any(operator in command_string for operator in shell_operators)',
            'use_shell = any(part in shell_operators for part in self._shell_parts(command_string))')
    replace(cli, 'parts = re.split(f"({pattern})", command_string)',
            'parts = self._shell_parts(command_string)')
    replace(cli, "env = os.environ.copy()",
            "env = os.environ.copy()\n"
            "            # Task scripts use the complete task Python, not the MCP server's minimal venv.\n"
            "            task_python = os.environ.get('PYTHON_BIN')\n"
            "            if task_python:\n"
            "                env['PATH'] = os.path.dirname(task_python) + os.pathsep + env.get('PATH', '')")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    patch(parser.parse_args().root)
    print('Applied Forms question-type and CLI Python-environment fixes; evaluators unchanged.')
