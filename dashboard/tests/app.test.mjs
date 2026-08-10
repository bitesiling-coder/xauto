import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const sourceUrl = new URL("../assets/app.js", import.meta.url);
const source = await readFile(sourceUrl, "utf8");
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const {
  bannerForState,
  formatMetric,
  isSafeExternalUrl,
  isStale,
  isValidSnapshot,
  loadSnapshotState,
  matchesFilter,
  snapshotUrl,
  sortPosts,
} = await import(moduleUrl);

const TOPICS = [
  { id: "ai-agents-security", label: "AI Agents 与 Agent Security", family: "AI" },
  { id: "world-models-embodied-ai", label: "World Models 与 Embodied AI", family: "AI" },
  { id: "rwa-stablecoin-payments", label: "RWA 与 Stablecoin Payments", family: "Web3" },
  {
    id: "prediction-markets-regulation",
    label: "Prediction Markets 与 Crypto Regulation",
    family: "Web3",
  },
];

function validSnapshot() {
  return {
    version: 1,
    generated_at: "2026-08-10T12:00:00+08:00",
    timezone: "Asia/Singapore",
    fallback_used: false,
    summary: { posts: 1, authors: 1, media: 1, engagement: 12 },
    topics: TOPICS.map((topic, index) => ({
      ...topic,
      posts: index === 0 ? 1 : 0,
      score: index === 0 ? 0.8 : 0,
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
        topic: "ai-agents-security",
        family: "AI",
        keywords: ["Agents"],
        score: 0.8,
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
  const zulu = validSnapshot();
  zulu.generated_at = "2026-08-10T04:00:00Z";
  zulu.posts[0].created_at = "2026-08-10T03:00:00Z";
  assert.equal(isValidSnapshot(zulu), true);
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

test("isValidSnapshot requires strictly valid RFC3339 calendar timestamps", () => {
  const invalidDates = [
    "2026-02-30T12:00:00Z",
    "2025-02-29T12:00:00Z",
    "2026-13-01T12:00:00Z",
    "2026-08-10T24:00:00Z",
    "2026-08-10T12:60:00Z",
    "2026-08-10T12:00:60Z",
    "2026-08-10T12:00:00+24:00",
    "2026-08-10 12:00:00Z",
    "2026-08-10T12:00:00",
  ];

  for (const generated_at of invalidDates) {
    assert.equal(isValidSnapshot({ ...validSnapshot(), generated_at }), false);
  }
  for (const created_at of invalidDates) {
    const payload = validSnapshot();
    payload.posts[0].created_at = created_at;
    assert.equal(isValidSnapshot(payload), false);
  }
});

test("isValidSnapshot rejects unsafe count and score values", () => {
  const invalidCounts = [-1, 1.5, Number.MAX_SAFE_INTEGER + 1, Number.NaN];
  for (const value of invalidCounts) {
    const summary = validSnapshot();
    summary.summary.posts = value;
    assert.equal(isValidSnapshot(summary), false);

    const post = validSnapshot();
    post.posts[0].likes = value;
    assert.equal(isValidSnapshot(post), false);

    const topic = validSnapshot();
    topic.topics[0].posts = value;
    assert.equal(isValidSnapshot(topic), false);
  }
  for (const score of [-0.01, 1.01, Number.NaN, Number.POSITIVE_INFINITY]) {
    const payload = validSnapshot();
    payload.posts[0].score = score;
    assert.equal(isValidSnapshot(payload), false);
  }
});

test("isValidSnapshot accepts only relative lowercase hashed media", () => {
  const invalidMedia = [
    { url: `assets/media/${"A".repeat(64)}.jpg`, type: "image", alt: "bad" },
    { url: `assets/media/${"a".repeat(64)}.jpg?x=1`, type: "image", alt: "bad" },
    { url: "assets/media/../private.jpg", type: "image", alt: "bad" },
    { url: "https://pbs.twimg.com/media/image.jpg", type: "image", alt: "bad" },
    { url: `assets/media/${"a".repeat(64)}.svg`, type: "image", alt: "bad" },
    { url: `assets/media/${"a".repeat(64)}.jpg`, type: "video", alt: "bad" },
  ];

  for (const media of invalidMedia) {
    const payload = validSnapshot();
    payload.posts[0].media = [media];
    assert.equal(isValidSnapshot(payload), false);
  }
  const poster = validSnapshot();
  poster.posts[0].media[0].type = "video_poster";
  assert.equal(isValidSnapshot(poster), true);
});

test("isValidSnapshot enforces approved topic identity and post references", () => {
  const malformed = [];
  const duplicate = validSnapshot();
  duplicate.topics[1].id = duplicate.topics[0].id;
  malformed.push(duplicate);
  const wrongFamily = validSnapshot();
  wrongFamily.topics[0].family = "Web3";
  malformed.push(wrongFamily);
  const wrongLabel = validSnapshot();
  wrongLabel.topics[0].label = "Agents";
  malformed.push(wrongLabel);
  const unknownTopic = validSnapshot();
  unknownTopic.posts[0].topic = "unknown";
  malformed.push(unknownTopic);
  const mismatchedPostFamily = validSnapshot();
  mismatchedPostFamily.posts[0].family = "Web3";
  malformed.push(mismatchedPostFamily);
  const badKeywords = validSnapshot();
  badKeywords.posts[0].keywords = [42];
  malformed.push(badKeywords);

  for (const payload of malformed) assert.equal(isValidSnapshot(payload), false);
});

test("isValidSnapshot enforces summary, fallback, and topic aggregates", () => {
  const mutations = [
    (payload) => (payload.summary.posts = 2),
    (payload) => (payload.summary.authors = 0),
    (payload) => (payload.summary.media = 0),
    (payload) => (payload.summary.engagement = 13),
    (payload) => (payload.fallback_used = true),
    (payload) => (payload.topics[0].posts = 0),
  ];

  for (const mutate of mutations) {
    const payload = validSnapshot();
    mutate(payload);
    assert.equal(isValidSnapshot(payload), false);
  }
});

test("isValidSnapshot compares distinct authors with Unicode casefolding", () => {
  const payload = validSnapshot();
  payload.posts[0].author = "Straße";
  payload.posts.push({
    ...structuredClone(payload.posts[0]),
    id: "post-2",
    author: "STRASSE",
    url: "https://x.com/ada/status/2",
    likes: 1,
    views: 1,
    media: [],
  });
  payload.summary = { posts: 2, authors: 1, media: 1, engagement: 14 };
  payload.topics[0].posts = 2;

  assert.equal(isValidSnapshot(payload), true);

  const greek = validSnapshot();
  greek.posts[0].author = "µ";
  greek.posts.push({
    ...structuredClone(greek.posts[0]),
    id: "post-2",
    author: "Μ",
    url: "https://x.com/ada/status/2",
    likes: 1,
    views: 1,
    media: [],
  });
  greek.summary = { posts: 2, authors: 1, media: 1, engagement: 14 };
  greek.topics[0].posts = 2;
  assert.equal(isValidSnapshot(greek), true);
});

test("matchesFilter supports all, AI, and Web3 only", () => {
  assert.equal(matchesFilter({ family: "AI" }, "all"), true);
  assert.equal(matchesFilter({ family: "AI" }, "AI"), true);
  assert.equal(matchesFilter({ family: "Web3" }, "AI"), false);
  assert.equal(matchesFilter({ family: "Web3" }, "Web3"), true);
  assert.equal(matchesFilter({ family: "AI" }, "unknown"), false);
});

test("loadSnapshotState returns newer only after strict validation", async () => {
  const current = validSnapshot();
  const candidate = validSnapshot();
  candidate.generated_at = "2026-08-10T12:30:00+08:00";
  const calls = [];
  const result = await loadSnapshotState({
    fetchSnapshot: async (...args) => {
      calls.push(args);
      return { ok: true, json: async () => candidate };
    },
    currentSnapshot: current,
    refreshed: true,
    clock: () => 123,
  });

  assert.equal(result.status, "newer");
  assert.equal(result.snapshot, candidate);
  assert.equal(result.refreshed, true);
  assert.deepEqual(calls, [["data/latest.json?t=123", { cache: "no-store" }]]);
});

test("loadSnapshotState preserves the current object when unchanged", async () => {
  const current = validSnapshot();
  const result = await loadSnapshotState({
    fetchSnapshot: async () => ({ ok: true, json: async () => structuredClone(current) }),
    currentSnapshot: current,
    refreshed: true,
  });

  assert.equal(result.status, "unchanged");
  assert.equal(result.snapshot, current);
});

test("loadSnapshotState preserves current for older or semantically equivalent data", async () => {
  const current = validSnapshot();
  const older = validSnapshot();
  older.generated_at = "2026-08-10T11:30:00+08:00";
  older.posts[0].text = "Older content";
  const reordered = Object.fromEntries(Object.entries(current).reverse());

  for (const candidate of [older, reordered]) {
    const result = await loadSnapshotState({
      fetchSnapshot: async () => ({ ok: true, json: async () => candidate }),
      currentSnapshot: current,
      refreshed: true,
    });
    assert.equal(result.status, "unchanged");
    assert.equal(result.snapshot, current);
  }
});

test("loadSnapshotState accepts changed content at the same timestamp", async () => {
  const current = validSnapshot();
  const changed = validSnapshot();
  changed.posts[0].text = "Changed content";

  const result = await loadSnapshotState({
    fetchSnapshot: async () => ({ ok: true, json: async () => changed }),
    currentSnapshot: current,
    refreshed: true,
  });
  assert.equal(result.status, "newer");
  assert.equal(result.snapshot, changed);
});

test("loadSnapshotState preserves existing state on fetch or validation failure", async () => {
  const current = validSnapshot();
  const invalid = validSnapshot();
  invalid.posts[0].media[0].url = "javascript:alert(1)";
  for (const fetchSnapshot of [
    async () => {
      throw new Error("offline");
    },
    async () => ({ ok: false, status: 503 }),
    async () => ({ ok: true, json: async () => invalid }),
  ]) {
    const result = await loadSnapshotState({
      fetchSnapshot,
      currentSnapshot: current,
      refreshed: true,
    });
    assert.equal(result.status, "failed-with-existing");
    assert.equal(result.snapshot, current);
  }
});

test("loadSnapshotState reports failed when no prior snapshot exists", async () => {
  const result = await loadSnapshotState({
    fetchSnapshot: async () => {
      throw new Error("offline");
    },
    currentSnapshot: null,
  });
  assert.deepEqual(result, { status: "failed", snapshot: null, refreshed: false });
});

test("bannerForState applies stale then fallback then refresh-result priority", () => {
  const current = validSnapshot();
  const now = Date.parse("2026-08-10T13:00:00+08:00");
  assert.match(bannerForState(current, "unchanged", now).message, /当前已是最新数据/);
  assert.match(bannerForState(current, "newer", now).message, /已载入新数据/);
  assert.match(
    bannerForState(current, "failed-with-existing", now).message,
    /刷新失败，继续展示上次数据/,
  );

  const fallback = validSnapshot();
  fallback.fallback_used = true;
  fallback.posts[0].fallback = true;
  assert.match(bannerForState(fallback, "newer", now).message, /回溯样本/);

  const stale = validSnapshot();
  stale.generated_at = "2026-08-08T00:00:00+08:00";
  assert.match(bannerForState(stale, "failed-with-existing", now).message, /超过 26 小时/);
});

test("bannerForState distinguishes initial load from a user refresh", () => {
  const current = validSnapshot();
  const now = Date.parse("2026-08-10T13:00:00+08:00");
  const initial = bannerForState(current, "newer", now, false).message;
  const refreshed = bannerForState(current, "newer", now, true).message;

  assert.equal(initial, "已载入最新公开热点快照。");
  assert.equal(refreshed, "刷新成功，已载入新数据。");
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
