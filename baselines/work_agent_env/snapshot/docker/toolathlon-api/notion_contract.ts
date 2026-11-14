/** Normalize legacy JSON transport and implement fixture-side Notion list semantics. */
function jsonField(value: any, expected: 'object' | 'array', label: string): any {
  if (typeof value === 'string') {
    if (!value.trim()) return expected === 'array' ? [] : null;
    value = JSON.parse(value);
  }
  if (value === null) return value;
  if (expected === 'array' ? !Array.isArray(value) : typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON ${expected}`);
  }
  return value;
}

function richTextMetadata(value: any): any {
  if (Array.isArray(value)) return value.map(richTextMetadata);
  if (!value || typeof value !== 'object') return value;
  const result: Record<string, any> = {};
  for (const [key, child] of Object.entries(value)) result[key] = richTextMetadata(child);
  if (value.text && typeof value.text.content === 'string' && (!value.type || value.type === 'text')) {
    result.type = 'text';
    result.plain_text = value.text.content;
    result.href = value.text.link?.url ?? null;
  }
  return result;
}

export function normalizeParams(params: Record<string, any>, operation: string): Record<string, any> {
  const value = { ...params };
  for (const key of ['parent', 'properties', 'icon', 'cover', 'filter', 'sort']) {
    if (value[key] !== undefined) value[key] = jsonField(value[key], 'object', key);
  }
  for (const key of ['children', 'title', 'description', 'rich_text', 'sorts']) {
    if (value[key] !== undefined) value[key] = jsonField(value[key], 'array', key);
  }
  if (value.children) {
    value.children = value.children.map((block: any) => {
      const child = jsonField(block, 'object', 'child block');
      if (!child) throw new Error('Child block cannot be null');
      return normalizeParams(child, 'block');
    });
  }
  if (['post-page', 'patch-page'].includes(operation) && value.properties) {
    const properties = { ...value.properties };
    // The bundled schema formerly exposed the title value as the property map.
    if (Array.isArray(properties.title)) {
      const richText = properties.title;
      delete properties.type;
      properties.title = { type: 'title', title: richText };
    }
    for (const [name, property] of Object.entries(properties)) {
      if (property && typeof property === 'object' && !Array.isArray(property)) {
        const p = property as Record<string, any>;
        if (Array.isArray(p.title)) properties[name] = { ...p, type: 'title' };
      }
    }
    value.properties = properties;
  }
  return richTextMetadata(value);
}

export function pageList(results: any[], params: Record<string, any>): any {
  const size = params.page_size === undefined ? 100 : Number(params.page_size);
  if (!Number.isInteger(size) || size < 1 || size > 100) throw new Error('page_size must be 1..100');
  const cursor = params.start_cursor;
  const position = cursor ? results.findIndex(row => row.id === cursor) : -1;
  if (cursor && position < 0) throw new Error('Unknown start_cursor');
  const start = position + 1;
  const selected = results.slice(start, start + size);
  const more = start + size < results.length;
  return { object: 'list', results: selected, has_more: more,
           next_cursor: more ? selected[selected.length - 1].id : null };
}

function plainText(value: any): string {
  return Array.isArray(value) ? value.map(item => item.plain_text ?? item.text?.content ?? '').join('') : String(value ?? '');
}

function propertyValue(row: any, name: string, kind?: string): any {
  const property = row.properties?.[name];
  const type = kind ?? property?.type ?? Object.keys(property ?? {}).find(k => k !== 'id');
  const value = property?.[type ?? ''];
  if (type === 'title' || type === 'rich_text') return plainText(value);
  if (type === 'select' || type === 'status') return value?.name ?? null;
  if (type === 'multi_select') return (value ?? []).map((v: any) => v.name);
  if (type === 'relation') return (value ?? []).map((v: any) => v.id);
  if (type === 'date') return value?.start ?? null;
  return value;
}

export function matchesFilter(row: any, filter: any): boolean {
  if (!filter) return true;
  if (Array.isArray(filter.and)) return filter.and.every((f: any) => matchesFilter(row, f));
  if (Array.isArray(filter.or)) return filter.or.some((f: any) => matchesFilter(row, f));
  const kinds = ['title','rich_text','number','checkbox','select','multi_select','date','url','email','phone_number','status','relation'];
  const kind = kinds.find(k => filter[k] !== undefined);
  if (!filter.property || !kind) throw new Error('Unsupported Notion filter');
  const value = propertyValue(row, filter.property, kind);
  const condition = filter[kind];
  return Object.entries(condition).every(([operator, wanted]) => {
    const empty = value === undefined || value === null || value === '' || Array.isArray(value) && !value.length;
    switch (operator) {
      case 'is_empty': return wanted ? empty : !empty;
      case 'is_not_empty': return wanted ? !empty : empty;
      case 'equals': return value === wanted;
      case 'does_not_equal': return value !== wanted;
      case 'contains': return Array.isArray(value) ? value.includes(wanted) : String(value ?? '').includes(String(wanted));
      case 'does_not_contain': return Array.isArray(value) ? !value.includes(wanted) : !String(value ?? '').includes(String(wanted));
      case 'starts_with': return String(value ?? '').startsWith(String(wanted));
      case 'ends_with': return String(value ?? '').endsWith(String(wanted));
      case 'greater_than': return value > Number(wanted);
      case 'greater_than_or_equal_to': return value >= Number(wanted);
      case 'less_than': return value < Number(wanted);
      case 'less_than_or_equal_to': return value <= Number(wanted);
      case 'before': return value !== null && new Date(value) < new Date(String(wanted));
      case 'after': return value !== null && new Date(value) > new Date(String(wanted));
      case 'on_or_before': return value !== null && new Date(value) <= new Date(String(wanted));
      case 'on_or_after': return value !== null && new Date(value) >= new Date(String(wanted));
      default: throw new Error(`Unsupported Notion filter operator: ${operator}`);
    }
  });
}

export function sortPages(rows: any[], sorts: any[] = []): any[] {
  return rows.sort((a, b) => {
    for (const sort of sorts) {
      const av = sort.property ? propertyValue(a, sort.property) : a[sort.timestamp];
      const bv = sort.property ? propertyValue(b, sort.property) : b[sort.timestamp];
      if (av === bv) continue;
      const order = av == null ? -1 : bv == null ? 1 : av < bv ? -1 : 1;
      return sort.direction === 'descending' ? -order : order;
    }
    return String(a.id).localeCompare(String(b.id));
  });
}
