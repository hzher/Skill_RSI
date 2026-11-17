"""Bounded per-role generations, wall time and observed provider token usage."""
import time

class Budget:
    def __init__(self, *, wall_seconds=900, input_tokens=1_000_000, output_tokens=65536):
        self.start = time.monotonic()
        self.wall_seconds, self.input_limit, self.output_limit = wall_seconds, input_tokens, output_tokens
        self.input = self.output = 0
        self.missing_usage = 0

    def call(self, model, messages, tools, path):
        remaining = self.wall_seconds - (time.monotonic() - self.start)
        if remaining <= 0 or self.input >= self.input_limit or self.output >= self.output_limit:
            raise RuntimeError('Role wall or observed token budget exhausted')
        result = model.call(messages, tools, path, token_limit=self.output_limit - self.output,
                            timeout_limit=remaining)
        usage = result.get('usage') or {}
        incoming, outgoing = usage.get('prompt_tokens'), usage.get('completion_tokens')
        if type(incoming) is int and incoming >= 0 and type(outgoing) is int and outgoing >= 0:
            self.input += incoming
            self.output += outgoing
        else:
            self.missing_usage += 1
        if self.input > self.input_limit or self.output > self.output_limit or time.monotonic() - self.start > self.wall_seconds:
            raise RuntimeError('Role budget exceeded in the completed response; no returned tools executed')
        return result

    def result(self):
        return {'input_tokens': self.input if not self.missing_usage else None,
                'output_tokens': self.output if not self.missing_usage else None,
                'known_input_lower_bound': self.input, 'known_output_lower_bound': self.output,
                'missing_usage_responses': self.missing_usage, 'wall_seconds': time.monotonic() - self.start}
