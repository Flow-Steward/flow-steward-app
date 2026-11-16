/* GitHub Actions adapter: preflight all assets before immutable, retryable upload. */
const fs = require('node:fs');
const path = require('node:path');
const names = ['openapi-public.json', 'openapi-application.json', 'openapi-manifest.json'];

async function publishAssets({github, context, release, directory}) {
  const assets = await github.paginate(github.rest.repos.listReleaseAssets, {
    ...context.repo, release_id: release.id, per_page: 100,
  });
  const pending = [];
  // Inspect everything first: a mismatching retry never writes any release asset.
  for (const name of names) {
    const data = fs.readFileSync(path.join(directory, name));
    const matches = assets.filter(asset => asset.name === name);
    if (matches.length > 1) throw new Error(`Duplicate release asset: ${name}`);
    if (!matches.length) { pending.push({name, data}); continue; }
    const response = await github.rest.repos.getReleaseAsset({
      ...context.repo, asset_id: matches[0].id, headers: {accept: 'application/octet-stream'},
    });
    if (!Buffer.from(response.data).equals(data)) throw new Error(`Immutable release asset differs: ${name}`);
  }
  for (const {name, data} of pending) {
    await github.rest.repos.uploadReleaseAsset({
      ...context.repo, release_id: release.id, name, data,
      headers: {'content-type': 'application/json'},
    });
  }
}
module.exports = {publishAssets};
