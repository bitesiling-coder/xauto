import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const sourceUrl = new URL("../assets/app.js", import.meta.url);
const source = await readFile(sourceUrl, "utf8");
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const {
  formatMetric,
  isSafeExternalUrl,
  isStale,
  isValidSnapshot,
  snapshotUrl,
  sortPosts,
} = await import(moduleUrl);

function validSnapshot() {
  return {
    version: 1,
    generated_at: "2026-08-10T12:00:00+08:00",
    timezone: "Asia/Singapore",
    fallback_used: false,
    summary: { posts: 1, authors: 1, media: 1, engagement: 12 },
    topics: ["one", "two", "three", "four"].map((id, index) => ({
      id,
      label: `主题 ${index + 1}`,
      family: index < 2 ? "AI" : "Web3",
      posts: index === 0 ? 1 : 0,
      score: index === 0 ? 2.5 : 0,
      top_keyword: index === 0 ? "Agents" : "",
    })),
    posts: [
      {
        id: "post-1",
        author: "Ada",
        text: "Agent security",
        created_at: "2026-08-10T11:00:00+08:00",
        url: "https://x.com/ada/status/1",
        likes: 2,
        views: 10,
        topic: "one",
        family: "AI",
        keywords: ["Agents"],
        score: 2.5,
        fallback: false,
        media: [
          {
            url: `assets/media/${"a".repeat(64)}.jpg`,
            type: "image",
            alt: "Agent security",
          },
        ],
      },
    ],
  };
}

test("formatMetric keeps values below 1000 as integers", () => {
  assert.equal(formatMetric(0), "0");
  assert.equal(formatMetric(999), "999");
});

test("formatMetric compacts thousands and removes trailing .0", () => {
  assert.equal(formatMetric(1000), "1K");
  assert.equal(formatMetric(1500), "1.5K");
  assert.equal(formatMetric(10_000), "10K");
});

test("formatMetric compacts millions", () => {
  assert.equal(formatMetric(1_000_000), "1M");
  assert.equal(formatMetric(1_250_000), "1.3M");
  assert.equal(formatMetric(999_999), "1M");
});

test("sortPosts orders by score without mutating its input", () => {
  const posts = [
    { id: "low", score: 2 },
    { id: "high", score: 8 },
  ];
  const original = structuredClone(posts);

  assert.deepEqual(sortPosts(posts, "score").map(({ id }) => id), ["high", "low"]);
  assert.deepEqual(posts, original);
  assert.notEqual(sortPosts(posts, "score"), posts);
});

test("sortPosts orders newest dates first", () => {
  const posts = [
    { id: "older", created_at: "2026-08-08T12:00:00+08:00" },
    { id: "newer", created_at: "2026-08-10T12:00:00+08:00" },
  ];

  assert.deepEqual(sortPosts(posts, "newest").map(({ id }) => id), ["newer", "older"]);
});

test("sortPosts orders engagement by views plus likes", () => {
  const posts = [
    { id: "quiet", views: 10, likes: 2 },
    { id: "popular", views: 8, likes: 9 },
  ];

  assert.deepEqual(sortPosts(posts, "engagement").map(({ id }) => id), [
    "popular",
    "quiet",
  ]);
});

test("sortPosts preserves input order for deterministic ties", () => {
  const tied = [
    { id: "first", score: 3, created_at: "2026-08-10T00:00:00Z", views: 2, likes: 1 },
    { id: "second", score: 3, created_at: "2026-08-10T00:00:00Z", views: 1, likes: 2 },
  ];

  for (const mode of ["score", "newest", "engagement", "unknown"]) {
    assert.deepEqual(sortPosts(tied, mode).map(({ id }) => id), ["first", "second"]);
  }
});

test("snapshotUrl cache-busts the latest snapshot", () => {
  assert.equal(snapshotUrl(123), "data/latest.json?t=123");
});

test("isSafeExternalUrl permits only credential-free HTTPS X links", () => {
  assert.equal(isSafeExternalUrl("https://x.com/ada/status/1"), true);
  assert.equal(isSafeExternalUrl("https://twitter.com/ada/status/1"), true);
  assert.equal(isSafeExternalUrl("javascript:alert(1)"), false);
  assert.equal(isSafeExternalUrl("http://x.com/ada/status/1"), false);
  assert.equal(isSafeExternalUrl("https://example.com/status/1"), false);
  assert.equal(isSafeExternalUrl("https://user@x.com/status/1"), false);
});

test("isValidSnapshot accepts the complete public contract", () => {
  assert.equal(isValidSnapshot(validSnapshot()), true);
});

test("isValidSnapshot rejects malformed nested records and unsafe URLs", () => {
  const cases = [
    { ...validSnapshot(), posts: [null] },
    { ...validSnapshot(), summary: { posts: 1 } },
    { ...validSnapshot(), topics: validSnapshot().topics.slice(0, 3) },
    {
      ...validSnapshot(),
      posts: [{ ...validSnapshot().posts[0], url: "javascript:alert(1)" }],
    },
    {
      ...validSnapshot(),
      posts: [
        {
          ...validSnapshot().posts[0],
          media: [{ url: "../private.jpg", type: "image", alt: "bad" }],
        },
      ],
    },
    { ...validSnapshot(), generated_at: "not-a-date" },
    { ...validSnapshot(), timezone: "Invalid/Timezone" },
    { ...validSnapshot(), summary: { ...validSnapshot().summary, posts: null } },
    { ...validSnapshot(), summary: { ...validSnapshot().summary, authors: false } },
    { ...validSnapshot(), summary: { ...validSnapshot().summary, media: "" } },
    { ...validSnapshot(), summary: { ...validSnapshot().summary, engagement: [] } },
  ];

  for (const payload of cases) assert.equal(isValidSnapshot(payload), false);
});

test("isStale becomes true only after 26 hours", () => {
  const now = Date.parse("2026-08-10T12:00:00Z");
  const atThreshold = new Date(now - 26 * 60 * 60 * 1000).toISOString();
  const pastThreshold = new Date(now - (26 * 60 * 60 * 1000 + 1)).toISOString();

  assert.equal(isStale(atThreshold, now), false);
  assert.equal(isStale(pastThreshold, now), true);
  assert.equal(isStale(new Date(now + 1000).toISOString(), now), false);
  assert.equal(isStale("not-a-date", now), false);
});
