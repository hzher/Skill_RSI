"""Delay frozen-judge JSON grammar until the native thinking channel closes.

The wrapper is dependency-free. xgrammar/transformers are imported only inside
the actual model-serving process when constructing a schema processor.
"""


class AfterThinking:
    def __init__(self, processor, *, prompt_length, closing_ids):
        if not closing_ids:
            raise ValueError('Thinking terminator token IDs required')
        self.processor, self.prompt_length = processor, prompt_length
        self.closing_ids, self.active = list(closing_ids), False

    def __call__(self, input_ids, scores):
        row = input_ids[0]
        tokens = row.tolist() if hasattr(row, 'tolist') else list(row)
        generated = tokens[self.prompt_length:]
        if not self.active:
            width = len(self.closing_ids)
            self.active = any(generated[i:i+width] == self.closing_ids for i in range(len(generated)-width+1))
        return self.processor(input_ids, scores) if self.active else scores


def schema_processor(tokenizer, *, vocab_size, schema, prompt_length, thinking=True):
    import xgrammar as xgr
    from xgrammar.contrib.hf import LogitsProcessor
    info = xgr.TokenizerInfo.from_huggingface(tokenizer, vocab_size=vocab_size)
    compiled = xgr.GrammarCompiler(info).compile_json_schema(schema)
    processor = LogitsProcessor(compiled)
    return AfterThinking(processor, prompt_length=prompt_length,
        closing_ids=tokenizer.encode('</think>', add_special_tokens=False)) if thinking else processor
