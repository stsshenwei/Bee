import assert from "node:assert/strict";
import test from "node:test";
import {
  deleteMarketplacePackage,
  getMarketplacePackage,
  getMarketplaceSnapshotVersion,
  listMarketplacePackages,
  marketplaceAuthHeaders,
  purgeMarketplaceVersion,
  publishMarketplaceVersion,
  restoreMarketplaceVersion,
  updateMarketplacePackage,
  validateMarketplaceBundle,
  yankMarketplaceVersion,
} from "./api.ts";
import { readFileSync } from "node:fs";

const bundleBytes = readFileSync(new URL("./marketplace-api.test.mjs", import.meta.url));

function jsonResponse(body, init = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status || 200,
    headers: { "Content-Type": "application/json" },
  });
}

test("marketplace auth headers omit anonymous bearer", () => {
  assert.deepEqual(marketplaceAuthHeaders(null), {});
  assert.deepEqual(marketplaceAuthHeaders("tok-1"), { Authorization: "Bearer tok-1" });
});

test("lists marketplace packages with filters and token", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      items: [
        {
          name: "code-review",
          owner: "alice",
          description: "评审技能包",
          category: "dev-workflow",
          keywords: ["review"],
          visibility: "public",
          latest_version: "1.0.0",
          created_at: "",
          updated_at: "",
          download_url: "",
          manageable: true,
          components: { skills: ["review"], mcp: true },
        },
      ],
    });
  };

  const data = await listMarketplacePackages({
    owner: "alice",
    q: "review",
    category: "dev-workflow",
    token: "tok-alice",
  });

  assert.match(calls[0].url, /\/marketplace\/packages\?owner=alice&q=review&category=dev-workflow$/);
  assert.equal(calls[0].options.headers.Authorization, "Bearer tok-alice");
  assert.equal(data.items[0].latest_version, "1.0.0");
  assert.equal(data.items[0].components.skills[0], "review");
});

test("gets package detail with encoded owner and name", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      name: "code-review",
      owner: "team x",
      description: "",
      category: "",
      keywords: [],
      visibility: "public",
      latest_version: "1.0.0",
      created_at: "",
      updated_at: "",
      download_url: "",
      manageable: false,
      versions: [{ package: "code-review", owner: "team x", version: "1.0.0", status: "published" }],
    });
  };

  const detail = await getMarketplacePackage("team x", "code-review", null);

  assert.match(calls[0].url, /\/marketplace\/packages\/team%20x\/code-review$/);
  assert.equal(detail.versions.length, 1);
  assert.equal(detail.versions[0].status, "published");
});

test("updates visibility and deletes package with bearer token", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    if (options.method === "PATCH") {
      return jsonResponse({ name: "code-review", owner: "alice", visibility: "private" });
    }
    return jsonResponse({ deleted: true, package: "code-review", owner: "alice" });
  };

  const updated = await updateMarketplacePackage("alice", "code-review", { visibility: "private" }, "tok");
  const removed = await deleteMarketplacePackage("alice", "code-review", "tok");

  assert.equal(calls[0].options.method, "PATCH");
  assert.equal(JSON.parse(calls[0].options.body).visibility, "private");
  assert.equal(updated.visibility, "private");
  assert.equal(calls[1].options.method, "DELETE");
  assert.equal(removed.deleted, true);
});

test("validate preview reports manifest result", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      valid: true,
      name: "code-review",
      version: "2.0.0",
      description: "",
      category: "",
      keywords: [],
      manifest: {},
      components: { skills: ["review"] },
      file_count: 2,
      total_uncompressed_bytes: 10,
      error: "",
    });
  };

  const file = new File([bundleBytes], "bundle.zip", { type: "application/zip" });
  const result = await validateMarketplaceBundle(file);

  assert.match(calls[0].url, /\/marketplace\/validate$/);
  assert.equal(result.valid, true);
  assert.equal(result.version, "2.0.0");
});

test("publishes version with form fields and token", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      package: "code-review",
      owner: "alice",
      version: "3.0.0",
      manifest: {},
      components: {},
      content_hash: "abc",
      size_bytes: 10,
      status: "published",
      published_at: "2026-09-12T00:00:00Z",
    });
  };

  const file = new File([bundleBytes], "bundle.zip", { type: "application/zip" });
  const record = await publishMarketplaceVersion("alice", "code-review", file, {
    version: "3.0.0",
    visibility: "public",
    token: "tok",
  });

  assert.match(calls[0].url, /\/marketplace\/packages\/alice\/code-review\/versions$/);
  assert.equal(calls[0].options.headers.Authorization, "Bearer tok");
  const form = calls[0].options.body;
  assert.equal(form.get("version"), "3.0.0");
  assert.equal(form.get("visibility"), "public");
  assert.equal(form.get("file").name, "bundle.zip");
  assert.equal(record.status, "published");
});

test("yank restore and purge hit the version lifecycle routes", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    if (String(url).endsWith("/restore")) {
      return jsonResponse({ package: "code-review", owner: "alice", version: "1.0.0", status: "published" });
    }
    if (String(url).includes("mode=purge")) {
      return jsonResponse({ purged: true, package: "code-review", version: "1.0.0" });
    }
    return jsonResponse({ package: "code-review", version: "1.0.0", status: "yanked" });
  };

  const yanked = await yankMarketplaceVersion("alice", "code-review", "1.0.0", "tok");
  const restored = await restoreMarketplaceVersion("alice", "code-review", "1.0.0", "tok");
  const purged = await purgeMarketplaceVersion("alice", "code-review", "1.0.0", "tok");

  assert.match(calls[0].url, /\/versions\/1\.0\.0$/);
  assert.equal(calls[0].options.method, "DELETE");
  assert.equal(yanked.status, "yanked");
  assert.match(calls[1].url, /\/versions\/1\.0\.0\/restore$/);
  assert.equal(calls[1].options.method, "POST");
  assert.equal(restored.status, "published");
  assert.match(calls[2].url, /\/versions\/1\.0\.0\?mode=purge$/);
  assert.equal(purged.purged, true);
});

test("snapshot version endpoint returns revision payload", async () => {
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push({ url: String(url) });
    return jsonResponse({ revision: "abc123", built_at: "2026-09-12T00:00:00Z", package_count: 2 });
  };

  const snapshot = await getMarketplaceSnapshotVersion();

  assert.match(calls[0].url, /\/marketplace\/snapshot\/version$/);
  assert.equal(snapshot.revision, "abc123");
  assert.equal(snapshot.package_count, 2);
});
