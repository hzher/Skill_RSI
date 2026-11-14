"""Preserve public Calendar attendee, recurrence, and reminder fields end to end."""
from pathlib import Path

path=Path('/opt/local_servers/Calendar-Autoauth-MCP-Server/src/index.ts')
source=path.read_text()
marker='// Schema definitions\n'
extras='''// Schema definitions
const CalendarExtraFields = {
    attendees: z.array(z.object({
        email: z.string().email(),
        displayName: z.string().optional(),
        responseStatus: z.enum(['needsAction', 'declined', 'tentative', 'accepted']).optional(),
    })).optional().describe("Event participants using their email addresses"),
    recurrence: z.array(z.string()).optional().describe("Recurrence rules such as RRULE strings"),
    reminders: z.object({
        useDefault: z.boolean().optional(),
        overrides: z.array(z.object({method: z.enum(['email','popup']), minutes: z.number().int().nonnegative()})).optional(),
    }).optional().describe("Event reminder settings"),
};
'''
if 'const CalendarExtraFields' not in source:
    assert source.count(marker)==1
    source=source.replace(marker,extras)
    for schema in ['CreateEventSchema','UpdateEventSchema']:
        anchor=f'const {schema} = z.object({{'
        assert source.count(anchor)==1
        source=source.replace(anchor,anchor+'\n    ...CalendarExtraFields,')
    old='INSERT INTO gcal.events (summary, description, location, start_datetime, start_timezone, end_datetime, end_timezone)'
    new='INSERT INTO gcal.events (summary, description, location, start_datetime, start_timezone, end_datetime, end_timezone, attendees, recurrence, reminders)'
    assert source.count(old)==1
    source=source.replace(old,new)
    source=source.replace('VALUES ($1, $2, $3, $4, $5, $6, $7)','VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10::jsonb)')
    anchor='                        endTimeZone,\n'
    assert source.count(anchor)==1
    source=source.replace(anchor,anchor+'''                        JSON.stringify(requestBody.attendees || []),
                        JSON.stringify(requestBody.recurrence || []),
                        JSON.stringify(requestBody.reminders || {}),
''')
    anchor='                // Always update the updated timestamp'
    assert source.count(anchor)==1
    source=source.replace(anchor,'''                for (const field of ['attendees', 'recurrence', 'reminders']) {
                    if (requestBody[field] !== undefined) {
                        setClauses.push(`${field} = $${paramIndex++}::jsonb`);
                        values.push(JSON.stringify(requestBody[field]));
                    }
                }

'''+anchor)
    path.write_text(source)
print('Calendar public attendee, recurrence, and reminder fields preserved on create/update')
