"""Repair observed fixture routes without modifying task data or evaluators."""
from pathlib import Path


def replace(path, old, new):
    text = path.read_text()
    if new in text:
        return
    if text.count(old) != 1:
        raise RuntimeError(f'Unexpected source: {path}')
    path.write_text(text.replace(old, new))


root = Path('/opt/local_servers')
replace(root/'mcp-canvas-lms/src/client.ts',
        'const simpleClient = newAxios.create({',
        'const simpleClient = process.env.PG_HOST ? this.client : newAxios.create({')

router = root/'mcp-canvas-lms/src/pg-canvas-router.ts'
anchor = '''    const params = config?.params || {};

    try {'''
branch = '''    const params = config?.params || {};

    // Preserve pagination when the account-user client uses the fixture backend.
    if (/^accounts\\/\\d+\\/users$/.test(path)) {
      const page = Math.max(1, parseInt(params.page) || 1);
      const perPage = Math.max(1, Math.min(20, parseInt(params.per_page) || 20));
      const values: any[] = [];
      let where = '';
      if (params.search_term) {
        values.push(`%${params.search_term}%`);
        where = ' WHERE name ILIKE $1 OR email ILIKE $1 OR login_id ILIKE $1';
      }
      const count = await this.pool.query('SELECT count(*)::int AS total FROM canvas.users' + where, values);
      const totalPages = Math.max(1, Math.ceil(count.rows[0].total / perPage));
      const result = await this.pool.query('SELECT * FROM canvas.users' + where +
        ` ORDER BY id LIMIT $${values.length + 1} OFFSET $${values.length + 2}`,
        [...values, perPage, (page - 1) * perPage]);
      const links: string[] = [];
      const link = (p: number, rel: string) => {
        const url = new URL('http://fixture.invalid/api/v1/' + path);
        url.searchParams.set('page', String(p));
        url.searchParams.set('per_page', String(perPage));
        links.push(`<${url}>; rel="${rel}"`);
      };
      if (page < totalPages) link(page + 1, 'next');
      if (page > 1) link(page - 1, 'prev');
      link(totalPages, 'last');
      return { ...makeResponse(result.rows), headers: { link: links.join(',') } };
    }

    try {'''
replace(router, anchor, branch)

replace(root/'woocommerce-mcp/src/services/pg-rest-router.ts',
        "const orderBy = params.orderby || 'id';",
        "const requestedOrder = params.orderby || 'id';\n"
        "    const orderBy = requestedOrder === 'date' ? 'date_created' :\n"
        "      requestedOrder === 'modified' ? 'date_modified' : requestedOrder;")
print('Patched Canvas account-user fixture pagination and WooCommerce date sorting')
