"""Preserve Notion metadata, Forms labels, and explicitly requested email senders."""
from pathlib import Path
import shutil


def replace(path, old, new):
    text=path.read_text()
    if new in text:
        return
    if text.count(old)!=1:
        raise RuntimeError(f'Unexpected source: {path}: {old[:70]}')
    path.write_text(text.replace(old,new))


root=Path('/opt/local_servers')
notion=root/'notion-mcp-server/src/openapi-mcp-server/client'
shutil.copy2('/tmp/notion_contract.ts',notion/'notion_contract.ts')
replace(notion/'pg-client.ts',
        'const { block_data, parent_type, parent_id, position, ...rest } = row',
        'const { block_data, parent_type, parent_id, position, block_type, content, page_id, ...rest } = row')
forms=root/'google-forms-mcp/src/pg-forms.ts'
replace(forms,'options: options.map((option: string) => ({ value: option })),','options: [...options],')
replace(forms,'questionData.choiceQuestion = q.config;',
        "questionData.choiceQuestion = { ...q.config, options: (q.config.options || []).map((option: any) => typeof option === 'string' ? { value: option } : option) };")

emails=root/'emails-mcp/src/emails_mcp'
tool=emails/'tools/email_tools.py'
text=tool.read_text()
start=text.index('    async def send_email(')
end=text.index('    @mcp.tool()',start)
part=text[start:end]
old='cc: str = None, bcc: str = None, attachments: List[str] = None) -> str:'
if 'from_addr: str = None' not in part:
    assert part.count(old)==1
    part=part.replace(old,'cc: str = None, bcc: str = None, attachments: List[str] = None, from_addr: str = None) -> str:')
    part=part.replace('            attachments: List of file paths to attach (optional)',
                      '            attachments: List of file paths to attach (optional)\n            from_addr: Sender address explicitly requested by the user (optional; otherwise configured account)')
    part=part.replace('attachments=attachments\n','attachments=attachments,\n                from_addr=from_addr\n')
    tool.write_text(text[:start]+part+text[end:])

service=emails/'services/email_service.py'
replace(service,'save_to_sent: bool = True) -> bool:',
        'save_to_sent: bool = True, from_addr: Optional[str] = None) -> bool:')
text=service.read_text()
start=text.index('    def send_email(')
end=text.index('\n    def ',start+10)
part=text[start:end]
if "**({'from_addr': from_addr}" not in part:
    assert part.count('attachments=attachments\n')==1
    part=part.replace('attachments=attachments\n',"attachments=attachments,\n                **({'from_addr': from_addr} if from_addr is not None else {})\n")
    service.write_text(text[:start]+part+text[end:])

backend=emails/'backends/pg_backend.py'
text=backend.read_text()
start=text.index('    def send_email(',text.index('class PgSMTPBackend'))
end=text.index('\n    def ',start+10)
part=text[start:end]
if 'from_addr: Optional[str] = None' not in part:
    part=part.replace('attachments: Optional[List[str]] = None,','attachments: Optional[List[str]] = None,\n        from_addr: Optional[str] = None,')
    part=part.replace('self.config.email','sender_email')
    part=part.replace('        self.ensure_connected()','''        self.ensure_connected()
        sender_email = self.config.email
        if from_addr is not None:
            from email.utils import parseaddr
            if '\\n' in from_addr or '\\r' in from_addr:
                raise ValueError('Invalid sender address')
            _, sender_email = parseaddr(from_addr)
            if not sender_email or '@' not in sender_email:
                raise ValueError('Invalid sender address')''',1)
    backend.write_text(text[:start]+part+text[end:])
print('Notion rich-text metadata, block response fields, Forms labels and explicit email sender patched')
