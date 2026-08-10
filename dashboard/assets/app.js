const STALE_AFTER_MS = 26 * 60 * 60 * 1000;
const RFC3339_PATTERN = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|([+-])(\d{2}):(\d{2}))$/;
const LOCAL_MEDIA_PATTERN = /^assets\/media\/[a-f0-9]{64}\.(?:jpg|jpeg|png|gif|webp)$/;
const MEDIA_TYPES = new Set(["image", "video_poster"]);
const APPROVED_TOPICS = new Map([
  ["ai-agents-security", { label: "AI Agents 与 Agent Security", family: "AI" }],
  ["world-models-embodied-ai", { label: "World Models 与 Embodied AI", family: "AI" }],
  ["rwa-stablecoin-payments", { label: "RWA 与 Stablecoin Payments", family: "Web3" }],
  [
    "prediction-markets-regulation",
    { label: "Prediction Markets 与 Crypto Regulation", family: "Web3" },
  ],
]);

export function formatMetric(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "0";
  const absolute = Math.abs(numeric);
  if (absolute < 1000) return Math.round(numeric).toLocaleString("zh-CN");

  const divisor = absolute >= 1_000_000 ? 1_000_000 : 1000;
  const suffix = divisor === 1_000_000 ? "M" : "K";
  const compact = Math.round((numeric / divisor) * 10) / 10;
  if (suffix === "K" && Math.abs(compact) >= 1000) {
    return `${Math.round((numeric / 1_000_000) * 10) / 10}M`;
  }
  return `${compact}${suffix}`;
}

export function sortPosts(posts, mode = "score") {
  const metric = mode === "newest" ? "newest" : mode === "engagement" ? "engagement" : "score";
  return posts
    .map((post, index) => ({ post, index }))
    .sort((left, right) => {
      let difference = 0;
      if (metric === "newest") {
        difference = safeTimestamp(right.post.created_at) - safeTimestamp(left.post.created_at);
      } else if (metric === "engagement") {
        difference = engagement(right.post) - engagement(left.post);
      } else {
        difference = safeNumber(right.post.score) - safeNumber(left.post.score);
      }
      return difference || left.index - right.index;
    })
    .map(({ post }) => post);
}

export function snapshotUrl(timestamp) {
  return `data/latest.json?t=${timestamp}`;
}

export function isStale(generatedAt, now = Date.now()) {
  const generated = Date.parse(generatedAt);
  const current = now instanceof Date ? now.getTime() : Number(now);
  return Number.isFinite(generated) && Number.isFinite(current) && current - generated > STALE_AFTER_MS;
}

export function isSafeExternalUrl(value) {
  if (typeof value !== "string" || value !== value.trim()) return false;
  try {
    const parsed = new URL(value);
    return (
      parsed.protocol === "https:" &&
      parsed.username === "" &&
      parsed["password"] === "" &&
      (parsed.port === "" || parsed.port === "443") &&
      ["x.com", "www.x.com", "twitter.com", "www.twitter.com"].includes(
        parsed.hostname.toLowerCase(),
      )
    );
  } catch {
    return false;
  }
}

export function isValidSnapshot(payload) {
  if (!isRecord(payload)) return false;
  if (
    payload.version !== 1 ||
    !validDate(payload.generated_at) ||
    !validTimezone(payload.timezone) ||
    typeof payload.fallback_used !== "boolean" ||
    !validSummary(payload.summary) ||
    !Array.isArray(payload.topics) ||
    payload.topics.length !== 4 ||
    !Array.isArray(payload.posts) ||
    payload.posts.length === 0
  ) {
    return false;
  }
  const topics = new Map();
  for (const topic of payload.topics) {
    if (!validTopic(topic) || topics.has(topic.id)) return false;
    topics.set(topic.id, topic);
  }
  if (
    topics.size !== APPROVED_TOPICS.size ||
    !payload.posts.every((post) => validPost(post, topics))
  ) {
    return false;
  }
  return validAggregates(payload, topics);
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validDate(value) {
  if (typeof value !== "string") return false;
  const match = RFC3339_PATTERN.exec(value);
  if (!match) return false;
  const [, yearText, monthText, dayText, hourText, minuteText, secondText, zone, , offsetHourText, offsetMinuteText] = match;
  const [year, month, day, hour, minute, second] = [
    yearText,
    monthText,
    dayText,
    hourText,
    minuteText,
    secondText,
  ].map(Number);
  if (
    month < 1 ||
    month > 12 ||
    day < 1 ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  ) {
    return false;
  }
  if (zone !== "Z" && (Number(offsetHourText) > 23 || Number(offsetMinuteText) > 59)) {
    return false;
  }
  const calendar = new Date(0);
  calendar.setUTCFullYear(year, month - 1, day);
  calendar.setUTCHours(hour, minute, second, 0);
  return (
    calendar.getUTCFullYear() === year &&
    calendar.getUTCMonth() === month - 1 &&
    calendar.getUTCDate() === day &&
    calendar.getUTCHours() === hour &&
    calendar.getUTCMinutes() === minute &&
    calendar.getUTCSeconds() === second &&
    Number.isFinite(Date.parse(value))
  );
}

function validTimezone(value) {
  if (typeof value !== "string" || value === "") return false;
  try {
    new Intl.DateTimeFormat("zh-CN", { timeZone: value }).format(0);
    return true;
  } catch {
    return false;
  }
}

function validCount(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function validScore(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function validSummary(summary) {
  return (
    isRecord(summary) &&
    ["posts", "authors", "media", "engagement"].every((key) => validCount(summary[key]))
  );
}

function validTopic(topic) {
  if (!isRecord(topic) || typeof topic.id !== "string") return false;
  const approved = APPROVED_TOPICS.get(topic.id);
  return (
    approved !== undefined &&
    topic.label === approved.label &&
    topic.family === approved.family &&
    typeof topic.top_keyword === "string" &&
    validCount(topic.posts) &&
    validScore(topic.score)
  );
}

function validPost(post, topics) {
  if (!isRecord(post) || typeof post.topic !== "string") return false;
  const topic = topics.get(post.topic);
  return (
    topic !== undefined &&
    ["id", "author", "text", "family"].every((key) => typeof post[key] === "string") &&
    post.id.length > 0 &&
    post.family === topic.family &&
    validDate(post.created_at) &&
    isSafeExternalUrl(post.url) &&
    validCount(post.likes) &&
    validCount(post.views) &&
    validScore(post.score) &&
    typeof post.fallback === "boolean" &&
    Array.isArray(post.keywords) &&
    post.keywords.every((keyword) => typeof keyword === "string") &&
    Array.isArray(post.media) &&
    post.media.every(validMedia)
  );
}

function validMedia(media) {
  return (
    isRecord(media) &&
    MEDIA_TYPES.has(media.type) &&
    typeof media.alt === "string" &&
    isSafeMediaUrl(media.url)
  );
}

function isSafeMediaUrl(value) {
  return typeof value === "string" && LOCAL_MEDIA_PATTERN.test(value);
}

function validAggregates(payload, topics) {
  const mediaCount = payload.posts.reduce((total, post) => total + post.media.length, 0);
  const totalEngagement = payload.posts.reduce(
    (total, post) => total + post.views + post.likes,
    0,
  );
  if (
    !Number.isSafeInteger(mediaCount) ||
    !Number.isSafeInteger(totalEngagement) ||
    payload.summary.posts !== payload.posts.length ||
    payload.summary.authors > payload.summary.posts ||
    (payload.posts.length > 0 && payload.summary.authors < 1) ||
    payload.summary.media !== mediaCount ||
    payload.summary.engagement !== totalEngagement ||
    payload.fallback_used !== payload.posts.some((post) => post.fallback)
  ) {
    return false;
  }
  for (const [id, topic] of topics) {
    if (topic.posts !== payload.posts.filter((post) => post.topic === id).length) return false;
  }
  return true;
}

export function matchesFilter(post, filter) {
  if (filter === "all") return true;
  return (filter === "AI" || filter === "Web3") && post?.family === filter;
}

export async function loadSnapshotState({
  fetchSnapshot,
  currentSnapshot = null,
  refreshed = false,
  clock = Date.now,
}) {
  const existing = currentSnapshot && isValidSnapshot(currentSnapshot) ? currentSnapshot : null;
  try {
    const response = await fetchSnapshot(snapshotUrl(clock()), { cache: "no-store" });
    if (!response?.ok) throw new Error("snapshot request failed");
    const candidate = await response.json();
    if (!isValidSnapshot(candidate)) throw new Error("invalid snapshot");
    if (
      existing &&
      (Date.parse(candidate.generated_at) < Date.parse(existing.generated_at) ||
        stableSnapshot(candidate) === stableSnapshot(existing))
    ) {
      return { status: "unchanged", snapshot: existing, refreshed };
    }
    return { status: "newer", snapshot: candidate, refreshed };
  } catch {
    return existing
      ? { status: "failed-with-existing", snapshot: existing, refreshed }
      : { status: "failed", snapshot: null, refreshed };
  }
}

function stableSnapshot(value) {
  if (Array.isArray(value)) return `[${value.map(stableSnapshot).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableSnapshot(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export function bannerForState(snapshot, status, now = Date.now(), refreshed = true) {
  if (snapshot && isStale(snapshot.generated_at, now)) {
    return {
      message: "当前快照已超过 26 小时，内容可能不是最新，请尝试刷新。",
      tone: "warning",
    };
  }
  if (snapshot?.fallback_used) {
    return { message: "最新窗口样本不足，页面包含已标注的回溯样本。", tone: "warning" };
  }
  if (status === "unchanged") {
    return { message: "当前已是最新数据。", tone: "default" };
  }
  if (status === "newer") {
    return {
      message: refreshed ? "刷新成功，已载入新数据。" : "已载入最新公开热点快照。",
      tone: "default",
    };
  }
  if (status === "failed-with-existing") {
    return { message: "刷新失败，继续展示上次数据。", tone: "error" };
  }
  return { message: "暂时无法读取热点数据，请稍后点击“立即刷新”重试。", tone: "error" };
}

function safeNumber(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

function safeTimestamp(value) {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function engagement(post) {
  return safeNumber(post.views) + safeNumber(post.likes);
}

if (typeof document !== "undefined") {
  const elements = {
    refresh: document.getElementById("refresh-button"),
    updatedAt: document.getElementById("updated-at"),
    status: document.getElementById("status-banner"),
    lead: document.getElementById("lead-story"),
    summary: document.getElementById("summary-grid"),
    topics: document.getElementById("topic-grid"),
    feed: document.getElementById("hotspot-feed"),
    sort: document.getElementById("sort-select"),
    dialog: document.getElementById("post-dialog"),
    dialogContent: document.getElementById("dialog-content"),
    dialogClose: document.getElementById("dialog-close"),
    template: document.getElementById("post-template"),
    filters: [...document.querySelectorAll("[data-filter]")],
  };

  const state = {
    snapshot: null,
    filter: "all",
    sort: "score",
    dialogOpener: null,
  };

  function setStatus(message, tone = "default") {
    elements.status.textContent = message;
    elements.status.dataset.tone = tone;
  }

  function externalLink(label, url, className = "source-link") {
    if (!isSafeExternalUrl(url)) throw new Error("invalid external link");
    const link = document.createElement("a");
    link.className = className;
    link.textContent = label;
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }

  function dateLabel(value, timezone, includeTime = false) {
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) return "时间未知";
    const options = includeTime
      ? { dateStyle: "medium", timeStyle: "short", timeZone: timezone }
      : { month: "short", day: "numeric", timeZone: timezone };
    try {
      return new Intl.DateTimeFormat("zh-CN", options).format(parsed);
    } catch {
      return new Intl.DateTimeFormat("zh-CN", includeTime ? { dateStyle: "medium", timeStyle: "short" } : { month: "short", day: "numeric" }).format(parsed);
    }
  }

  function setPlaceholderSemantics(container, label) {
    container.setAttribute("role", "img");
    container.setAttribute("aria-label", label);
  }

  function clearPlaceholderSemantics(container) {
    container.removeAttribute("role");
    container.removeAttribute("aria-label");
  }

  function appendMedia(container, media, { priority = false, label = "热点媒体占位图" } = {}) {
    setPlaceholderSemantics(container, label);
    if (!media || !isSafeMediaUrl(media.url)) return;
    const image = document.createElement("img");
    image.src = media.url;
    image.alt = media.alt || "热点配图";
    image.loading = priority ? "eager" : "lazy";
    if (priority) image.fetchPriority = "high";
    image.decoding = "async";
    image.addEventListener("load", () => clearPlaceholderSemantics(container), { once: true });
    image.addEventListener("error", () => image.remove(), { once: true });
    container.append(image);
    if (image.complete && image.naturalWidth > 0) clearPlaceholderSemantics(container);
  }

  function conciseExcerpt(text, author) {
    const value = text.trim() || `@${author || "未知作者"} 的公开动态暂无文字摘要。`;
    return value.length > 180 ? `${value.slice(0, 179)}…` : value;
  }

  function topicLabel(post) {
    const topic = state.snapshot.topics.find(({ id }) => id === post.topic);
    return topic ? topic.label : post.topic || post.family || "热点";
  }

  function renderLead() {
    const post = sortPosts(state.snapshot.posts, "score")[0];
    const media = document.createElement("div");
    media.className = "lead-media media-placeholder";
    appendMedia(media, post.media?.[0], {
      priority: true,
      label: "领衔热点媒体占位图",
    });

    const content = document.createElement("div");
    content.className = "lead-content";
    const kicker = document.createElement("span");
    kicker.className = "lead-kicker";
    kicker.textContent = post.fallback ? `${topicLabel(post)} · 回溯样本` : topicLabel(post);
    const title = document.createElement("h3");
    title.textContent = `@${post.author || "未知作者"} · 今日领衔`;
    const excerpt = document.createElement("p");
    excerpt.className = "lead-excerpt";
    excerpt.textContent = conciseExcerpt(post.text, post.author);
    const meta = document.createElement("div");
    meta.className = "lead-meta";
    for (const label of [
      `@${post.author || "未知作者"}`,
      dateLabel(post.created_at, state.snapshot.timezone, true),
      `浏览 ${formatMetric(post.views)}`,
      `喜欢 ${formatMetric(post.likes)}`,
    ]) {
      const item = document.createElement("span");
      item.textContent = label;
      meta.append(item);
    }
    content.append(kicker, title, excerpt, meta, externalLink("查看 X 原帖 ↗", post.url));
    elements.lead.replaceChildren(media, content);
  }

  function renderSummary() {
    const definitions = [
      ["今日帖子", state.snapshot.summary.posts, "tone-green"],
      ["活跃作者", state.snapshot.summary.authors, "tone-blue"],
      ["媒体素材", state.snapshot.summary.media, "tone-purple"],
      ["总互动量", state.snapshot.summary.engagement, "tone-orange"],
    ];
    const cards = definitions.map(([label, value, tone]) => {
      const card = document.createElement("article");
      card.className = `metric-card ${tone}`;
      const name = document.createElement("p");
      name.className = "metric-label";
      name.textContent = label;
      const metric = document.createElement("p");
      metric.className = "metric-value";
      metric.textContent = formatMetric(value);
      card.append(name, metric);
      return card;
    });
    elements.summary.replaceChildren(...cards);
  }

  function renderTopics() {
    const tones = ["tone-green", "tone-blue", "tone-purple", "tone-orange"];
    const cards = state.snapshot.topics.map((topic, index) => {
      const card = document.createElement("article");
      card.className = `topic-card ${tones[index % tones.length]}`;
      const head = document.createElement("div");
      head.className = "topic-card-head";
      const identity = document.createElement("div");
      const title = document.createElement("h3");
      title.textContent = topic.label;
      const family = document.createElement("p");
      family.className = "topic-family";
      family.textContent = topic.family;
      identity.append(title, family);
      const score = document.createElement("span");
      score.className = "topic-score";
      score.textContent = safeNumber(topic.score).toFixed(1).replace(/\.0$/, "");
      head.append(identity, score);
      const stats = document.createElement("div");
      stats.className = "topic-stats";
      const count = document.createElement("span");
      const countStrong = document.createElement("strong");
      countStrong.textContent = formatMetric(topic.posts);
      count.append(countStrong, " 条动态");
      const keyword = document.createElement("span");
      const keywordStrong = document.createElement("strong");
      keywordStrong.textContent = topic.top_keyword || "暂无关键词";
      keyword.append("关键词 · ", keywordStrong);
      stats.append(count, keyword);
      card.append(head, stats);
      return card;
    });
    elements.topics.replaceChildren(...cards);
  }

  function fillPostCard(post) {
    const card = elements.template.content.firstElementChild.cloneNode(true);
    const media = card.querySelector(".post-media");
    appendMedia(media, post.media?.[0], { label: "热点卡片媒体占位图" });
    card.querySelector(".topic-pill").textContent = topicLabel(post);
    const fallback = card.querySelector(".fallback-badge");
    fallback.hidden = !post.fallback;
    card.querySelector(".post-excerpt").textContent = post.text || "该动态没有文字摘要。";
    card.querySelector(".post-author").textContent = `@${post.author || "未知作者"}`;
    const date = card.querySelector(".post-date");
    date.dateTime = post.created_at;
    date.textContent = dateLabel(post.created_at, state.snapshot.timezone);
    card.querySelector(".post-views").textContent = `浏览 ${formatMetric(post.views)}`;
    card.querySelector(".post-likes").textContent = `喜欢 ${formatMetric(post.likes)}`;
    const detail = card.querySelector(".detail-button");
    detail.setAttribute(
      "aria-label",
      `查看 @${post.author || "未知作者"} 的完整热点详情`,
    );
    detail.addEventListener("click", () => openDialog(post, detail));
    card.querySelector(".source-link").replaceWith(externalLink("查看原帖 ↗", post.url));
    return card;
  }

  function renderFeed() {
    const visible = state.snapshot.posts.filter(
      (post) => matchesFilter(post, state.filter),
    );
    const cards = sortPosts(visible, state.sort).map(fillPostCard);
    if (!cards.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "当前筛选条件下暂无热点，试试切换到其他主题。";
      elements.feed.replaceChildren(empty);
    } else {
      elements.feed.replaceChildren(...cards);
    }
  }

  function setBusy(value) {
    for (const region of [elements.lead, elements.summary, elements.topics, elements.feed]) {
      region.setAttribute("aria-busy", String(value));
    }
  }

  function openDialog(post, opener) {
    state.dialogOpener = opener;
    const heading = document.createElement("h2");
    heading.id = "dialog-heading";
    heading.className = "dialog-title";
    heading.textContent = `@${post.author || "未知作者"} · 热点详情`;
    const copy = document.createElement("p");
    copy.className = "dialog-copy";
    copy.textContent = post.text || "该动态没有文字内容。";
    const meta = document.createElement("div");
    meta.className = "dialog-meta";
    const labels = [
      `@${post.author || "未知作者"}`,
      dateLabel(post.created_at, state.snapshot.timezone, true),
      `浏览 ${formatMetric(post.views)}`,
      `喜欢 ${formatMetric(post.likes)}`,
      `热度 ${safeNumber(post.score).toFixed(1).replace(/\.0$/, "")}`,
    ];
    if (post.fallback) labels.push("回溯样本");
    for (const label of labels) {
      const item = document.createElement("span");
      item.textContent = label;
      meta.append(item);
    }
    const gallery = document.createElement("div");
    gallery.className = "dialog-gallery";
    gallery.setAttribute("aria-label", "全部媒体");
    if (post.media?.length) {
      for (const item of post.media) {
        const frame = document.createElement("div");
        frame.className = "dialog-media media-placeholder";
        appendMedia(frame, item, { label: "热点详情媒体占位图" });
        gallery.append(frame);
      }
    } else {
      const frame = document.createElement("div");
      frame.className = "dialog-media media-placeholder";
      setPlaceholderSemantics(frame, "该热点没有媒体素材");
      gallery.append(frame);
    }
    const keywords = document.createElement("div");
    keywords.className = "dialog-keywords";
    keywords.setAttribute("aria-label", "公开关键词");
    const publicKeywords = Array.isArray(post.keywords) ? post.keywords : [];
    if (publicKeywords.length) {
      for (const keyword of publicKeywords) {
        const chip = document.createElement("span");
        chip.className = "keyword-chip";
        chip.textContent = keyword;
        keywords.append(chip);
      }
    } else {
      const empty = document.createElement("span");
      empty.textContent = "暂无公开关键词";
      keywords.append(empty);
    }
    elements.dialogContent.replaceChildren(
      heading,
      meta,
      copy,
      gallery,
      keywords,
      externalLink("在 X 查看原帖 ↗", post.url),
    );
    elements.dialog.showModal();
  }

  function render(snapshot) {
    state.snapshot = snapshot;
    elements.updatedAt.dateTime = snapshot.generated_at;
    elements.updatedAt.textContent = dateLabel(snapshot.generated_at, snapshot.timezone, true);
    renderLead();
    renderSummary();
    renderTopics();
    renderFeed();

  }

  async function loadSnapshot(refreshed = false) {
    elements.refresh.disabled = true;
    setBusy(true);
    setStatus(refreshed ? "正在刷新热点数据…" : "正在加载最新热点数据…");
    try {
      const result = await loadSnapshotState({
        fetchSnapshot: fetch,
        currentSnapshot: state.snapshot,
        refreshed,
        clock: Date.now,
      });
      if (result.status === "newer") render(result.snapshot);
      const banner = bannerForState(result.snapshot, result.status, Date.now(), result.refreshed);
      setStatus(banner.message, banner.tone);
    } finally {
      setBusy(false);
      elements.refresh.disabled = false;
    }
  }

  elements.refresh.addEventListener("click", () => loadSnapshot(true));
  elements.sort.addEventListener("change", () => {
    state.sort = elements.sort.value;
    if (state.snapshot) renderFeed();
  });
  for (const button of elements.filters) {
    button.addEventListener("click", () => {
      state.filter = button.dataset.filter;
      for (const candidate of elements.filters) {
        const active = candidate === button;
        candidate.classList.toggle("is-active", active);
        candidate.setAttribute("aria-pressed", String(active));
      }
      if (state.snapshot) renderFeed();
    });
  }
  elements.dialogClose.addEventListener("click", () => elements.dialog.close());
  elements.dialog.addEventListener("click", (event) => {
    if (event.target === elements.dialog) elements.dialog.close();
  });
  elements.dialog.addEventListener("close", () => {
    state.dialogOpener?.focus();
    state.dialogOpener = null;
  });

  loadSnapshot();
}
