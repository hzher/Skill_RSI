"""Honor Canvas current-user submission includes and expose actual query errors."""
from pathlib import Path

path=Path('/opt/local_servers/mcp-canvas-lms/src/pg-canvas-router.ts')
source=path.read_text()

def replace(old,new):
    global source
    assert source.count(old)==1, old
    source=source.replace(old,new)

anchor='  // ---- GET routing ----'
replace(anchor,'''  private async withSubmission(rows: any[], params: any): Promise<any[]> {
    const include = params.include || params['include[]'] || [];
    const includes = Array.isArray(include) ? include : [include];
    if (!includes.includes('submission') || rows.length === 0) return rows;
    // Match the fixture's existing /submissions/self identity, not all students.
    const result = await this.pool.query(
      'SELECT * FROM canvas.submissions WHERE assignment_id = ANY($1::bigint[]) AND user_id = 1',
      [rows.map((row: any) => row.id)]
    );
    const submissions = new Map(result.rows.map((row: any) => [Number(row.assignment_id), row]));
    return rows.map((row: any) => ({...row, submission: submissions.get(Number(row.id)) || null}));
  }

'''+anchor)
replace("const res = await this.pool.query('SELECT * FROM canvas.assignments WHERE id = $1 AND course_id = $2', [m[2], m[1]]);\n      return res.rows[0] || {};",
        "const res = await this.pool.query('SELECT * FROM canvas.assignments WHERE id = $1 AND course_id = $2', [m[2], m[1]]);\n      return (await this.withSubmission(res.rows, params))[0] || {};")
replace("const res = await this.pool.query('SELECT * FROM canvas.assignments WHERE course_id = $1 ORDER BY position', [m[1]]);\n      return res.rows;",
        "const res = await this.pool.query('SELECT * FROM canvas.assignments WHERE course_id = $1 ORDER BY position', [m[1]]);\n      return this.withSubmission(res.rows, params);")
for verb,fallback in [('GET','makeResponse([], 200)'),('POST','makeResponse({}, 201)'),('PUT','makeResponse({})'),('DELETE','makeResponse({})')]:
    old=f'console.error(`[PgCanvasRouter] {verb} ${{path}} error:`, err.message);\n      return {fallback};'
    replace(old,f'console.error(`[PgCanvasRouter] {verb} ${{path}} error:`, err.message);\n      throw err;')
replace('console.error(`[PgCanvasRouter] Unmatched GET: ${path}`);\n    return [];',
        'throw new Error(`Unsupported Canvas fixture GET route: ${path}`);')
path.write_text(source)
