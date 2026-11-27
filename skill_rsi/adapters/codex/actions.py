"""Decode explicit model actions without recovering arbitrary nested JSON."""
import json
import re


def decode_action(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('executor returned no action text')
    text = text.strip()
    if '<｜｜DSML｜｜ calls>' in text:
        calls = []
        bodies = re.findall(r'<｜｜DSML｜｜ invoke name="([^"]+)">(.*?)</｜｜DSML｜｜ invoke>', text, re.S)
        for name, body in bodies:
            values = re.findall(
                r'<｜｜DSML｜｜ parameter name="arguments" string="false">(.*?)</｜｜DSML｜｜ parameter>', body, re.S)
            if len(values) != 1:
                raise ValueError('unsupported or ambiguous DSML arguments')
            arguments = json.loads(values[0])
            if not isinstance(arguments, dict):
                raise ValueError('tool arguments must be a JSON object')
            calls.append({'name': name, 'arguments': arguments})
        if not calls:
            raise ValueError('DSML text contains no explicit supported tool call')
        return {'tool_calls': calls, 'final_output': None}
    if text.startswith('```'):
        match = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', text, re.S)
        if not match:
            raise ValueError('ambiguous fenced action')
        text = match[1]
    try:
        action = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError('executor action must be one JSON object or explicit DSML calls') from error
    if not isinstance(action, dict):
        raise ValueError('executor action must be a JSON object')
    return action
