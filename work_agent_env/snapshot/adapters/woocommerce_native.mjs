// Project-side native verification adapter; upstream source stays unchanged.
// PgRestRouter exists upstream but BaseService still constructs a live axios client.
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import fs from 'node:fs';

const upstream = process.env.TOOLATHLON_ROOT;
if (!upstream) throw new Error('TOOLATHLON_ROOT is required');
const serverRoot = process.env.WOOCOMMERCE_NATIVE_ROOT;
if (!serverRoot) throw new Error('WOOCOMMERCE_NATIVE_ROOT is required');
const { default: axios } = await import(pathToFileURL(path.join(serverRoot, 'node_modules', 'axios', 'index.js')));
const { PgRestRouter } = await import(pathToFileURL(path.join(serverRoot, 'dist', 'services', 'pg-rest-router.js')));
axios.create = () => {
  const router = new PgRestRouter();
  // Upstream schema advertises status arrays but its SQL router uses scalar equality.
  // Preserve all other filters and pagination; add a parameterized ANY clause.
  const buildListQuery = router.buildListQuery.bind(router);
  router.buildListQuery = (table, params, parentKey, parentId) => {
    // The public order schema calls creation-time ordering "date"; the pinned
    // PostgreSQL schema stores it as date_created. Do not mutate caller input
    // or rewrite unrelated resources and fields.
    if (table === 'wc.orders' && params.orderby === 'date') {
      params = { ...params, orderby: 'date_created' };
    }
    if (!Array.isArray(params.status)) return buildListQuery(table, params, parentKey, parentId);
    const result = buildListQuery(table, { ...params, status: undefined }, parentKey, parentId);
    if (params.status.length) {
      const clause = `"status" = ANY($${result.values.length + 1}::text[])`;
      const join = result.query.includes(' WHERE ') ? ' AND ' : ' WHERE ';
      result.query = result.query.replace(' ORDER BY ', `${join}${clause} ORDER BY `);
      result.values.push(params.status);
    }
    return result;
  };
  router.interceptors = { request: { use() {} }, response: { use() {} } };
  return router;
};
// MCP stdout must contain only protocol messages, not startup diagnostics.
console.log = (...args) => console.error(...args);
// The upstream server strips JSON escape characters from product names, corrupting
// tool payloads containing quotes. Patch only this compiled expression in a sibling
// generated module; tracked upstream TypeScript is not edited.
const original = fs.readFileSync(path.join(serverRoot, 'dist', 'server.js'), 'utf8');
const badSerialization = /(JSON\.stringify\(result, null, 2\))\.replace\([^\n]+\)/g;
const matches = [...original.matchAll(badSerialization)];
if (matches.length !== 1) throw new Error('Pinned upstream serialization expression changed; inspect adapter');
let prepared = original.replace(badSerialization, '$1');
const shippingExtension = process.env.WORKENV_SHIPPING_READ_EXTENSIONS ?? '0';
if (!['0', '1'].includes(shippingExtension)) throw new Error('Invalid shipping read extension flag');
if (shippingExtension === '1') {
  // Project-labelled read-only registration of an existing upstream service
  // method. No new SQL router or mutation endpoint is introduced.
  const toolAnchor = /\/\/ Shipping tools\r?\n\s*\{/g;
  const callAnchor = /case 'woo_shipping_zones_list':/g;
  if ([...prepared.matchAll(toolAnchor)].length !== 1 || [...prepared.matchAll(callAnchor)].length !== 1) {
    throw new Error('Pinned shipping registration changed; inspect adapter');
  }
  prepared = prepared.replace(toolAnchor, `// Shipping tools
            { name: 'workenv_shipping_zone_locations_get',
              description: 'Project read-only registration of upstream shipping zone locations',
              inputSchema: { type: 'object', properties: { zoneId: { type: 'integer', minimum: 1 } }, required: ['zoneId'], additionalProperties: false } },
            {`);
  prepared = prepared.replace(callAnchor, `case 'workenv_shipping_zone_locations_get':
                    if (!Number.isSafeInteger(args.zoneId) || args.zoneId < 1) throw new Error('Invalid owned shipping zone ID');
                    result = await shippingService.getShippingZoneLocations(args.zoneId);
                    break;
                case 'woo_shipping_zones_list':`);
}
const resourceModule = process.env.WORKENV_WOOCOMMERCE_MODULE_ID;
if (resourceModule && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(resourceModule)) {
  throw new Error('Invalid isolated resource module identity');
}
const filename = resourceModule ? `server.memora-native-${resourceModule}-${process.pid}.js` : 'server.memora-native.js';
const patched = path.join(serverRoot, 'dist', filename);
fs.writeFileSync(patched, prepared, resourceModule ? { flag: 'wx' } : undefined);
if (resourceModule) process.on('exit', () => { try { fs.unlinkSync(patched); } catch {} });
const { startMcpServer } = await import(pathToFileURL(patched));
await startMcpServer();
