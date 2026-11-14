"""Execute the production query wrapper against a recording router, without a DB."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node is an optional native component')
class WooQueryAdapterTests(unittest.TestCase):
    def query(self, table, params, parent_key=None, parent_id=None):
        source = (ROOT/'adapters/woocommerce_native.mjs').read_text(encoding='utf-8')
        wrapper = source.split('axios.create = () => {', 1)[1].split('\n};', 1)[0]
        program = '''
const calls = [];
class PgRestRouter {
  buildListQuery(table, params, parentKey, parentId) {
    calls.push({table, params, parentKey, parentId});
    return {query: 'SELECT * FROM ' + table + (params.customer_id ? ' WHERE "customer_id" = $1' : '') +
      ' ORDER BY ' + (params.orderby || 'id') + ' LIMIT ' + (params.per_page || 10) + ' OFFSET 0',
      values: params.customer_id ? [params.customer_id] : []};
  }
}
const create = () => { WRAPPER };
const input = JSON.parse(process.argv[1]);
const before = JSON.stringify(input);
const result = create().buildListQuery(...input);
console.log(JSON.stringify({result, calls, unchanged:before === JSON.stringify(input)}));
'''.replace('WRAPPER', wrapper)
        result = subprocess.run(['node', '-e', program, json.dumps([table,params,parent_key,parent_id])],
                                capture_output=True, text=True, timeout=10, check=True)
        return json.loads(result.stdout)

    def test_public_date_order_uses_creation_column_without_mutating_input(self):
        result = self.query('wc.orders', {'orderby':'date','order':'asc','per_page':100,'status':'completed'})
        self.assertEqual(result['calls'][0]['params']['orderby'], 'date_created')
        self.assertEqual(result['calls'][0]['params']['status'], 'completed')
        self.assertIn('LIMIT 100', result['result']['query'])
        self.assertTrue(result['unchanged'])

    def test_date_alias_and_status_array_preserve_parameterized_filters(self):
        result = self.query('wc.orders', {'orderby':'date','status':['completed','processing'],'customer_id':7}, 'parent_id', 9)
        self.assertEqual(result['result']['values'], [7,['completed','processing']])
        self.assertIn(' AND "status" = ANY($2::text[]) ORDER BY date_created', result['result']['query'])
        self.assertEqual(result['calls'][0]['parentKey'], 'parent_id')
        self.assertEqual(result['calls'][0]['parentId'], 9)
        self.assertTrue(result['unchanged'])

    def test_other_fields_resources_and_empty_status_keep_upstream_behavior(self):
        for table, params in [('wc.orders',{'orderby':'id'}),('wc.products',{'orderby':'date'}),
                              ('wc.orders',{'status':[]})]:
            with self.subTest(table=table,params=params):
                result = self.query(table,params)
                self.assertEqual(result['calls'][0]['params'].get('orderby'),params.get('orderby'))
                self.assertNotIn('ANY(',result['result']['query'])
                self.assertTrue(result['unchanged'])


if __name__ == '__main__': unittest.main()
