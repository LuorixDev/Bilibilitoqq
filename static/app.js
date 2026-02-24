const container = document.getElementById("user-list");
let hasRendered = false;
const cards = new Map();

function createElement(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function formatCheckedAt(iso) {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString();
}

function formatTimestamp(ts) {
  if (!ts) return "-";
  const value = ts > 1e12 ? ts : ts * 1000;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(ts);
  return date.toLocaleString();
}

function truncate(text, max = 80) {
  if (!text) return "暂无动态";
  if (text.length <= max) return text;
  return `${text.slice(0, max)}...`;
}

function ensureCard(user) {
  let entry = cards.get(user.id);
  if (entry) return entry;

  const card = createElement("article", "card");
  const header = createElement("div", "card-header");
  const title = createElement("h3", null, "");
  const badge = createElement("span", "status", "");
  header.appendChild(title);
  header.appendChild(badge);

  const meta = createElement("div", "meta");
  const uid = createElement("div", "meta-item", "");
  const liveUrl = createElement("div", "meta-item", "");
  meta.appendChild(uid);
  meta.appendChild(liveUrl);

  const stats = createElement("div", "stats");
  const liveTitle = createElement("div", "stat", "");
  const liveOnline = createElement("div", "stat", "");
  const liveDuration = createElement("div", "stat", "");
  const dynamicId = createElement("div", "stat", "");
  const dynamicType = createElement("div", "stat", "");
  const dynamicTitleStat = createElement("div", "stat", "");
  const dynamicTime = createElement("div", "stat", "");
  const checked = createElement("div", "stat", "");
  const nextPoll = createElement("div", "stat", "");
  stats.appendChild(liveTitle);
  stats.appendChild(liveOnline);
  stats.appendChild(liveDuration);
  stats.appendChild(dynamicId);
  stats.appendChild(dynamicType);
  stats.appendChild(dynamicTitleStat);
  stats.appendChild(dynamicTime);
  stats.appendChild(checked);
  stats.appendChild(nextPoll);

  const dynamicTitle = createElement("div", "players-title", "最新动态");
  const dynamicText = createElement("div", "dynamic", "");

  card.appendChild(header);
  card.appendChild(meta);
  card.appendChild(stats);
  card.appendChild(dynamicTitle);
  card.appendChild(dynamicText);

  entry = {
    card,
    title,
    badge,
    uid,
    liveUrl,
    liveTitle,
    liveOnline,
    liveDuration,
    dynamicId,
    dynamicType,
    dynamicTitleStat,
    dynamicTime,
    checked,
    nextPoll,
    dynamicText,
    nextPollAt: null,
  };
  cards.set(user.id, entry);
  return entry;
}

function updateCard(entry, user) {
  const isLive = !!user.live;
  entry.card.className = `card ${isLive ? "online" : "offline"}`;
  entry.badge.className = `status ${isLive ? "online" : "offline"}`;
  entry.badge.textContent = isLive ? "直播中" : "未开播";
  entry.title.textContent = user.name || `UID ${user.uid}`;
  entry.uid.textContent = `UID：${user.uid}`;
  entry.liveUrl.textContent = user.live_url ? `直播间：${user.live_url}` : "";
  entry.liveTitle.textContent = user.live_title
    ? `直播标题：${user.live_title}`
    : "直播标题：-";
  entry.liveOnline.textContent = isLive
    ? `当前人气：${user.live_online ?? "-"}`
    : "当前人气：-";
  entry.liveDuration.textContent = isLive
    ? `直播时长：${user.live_duration || "-"}`
    : "直播时长：-";
  entry.dynamicId.textContent = user.last_dynamic_id
    ? `动态ID：${user.last_dynamic_id}`
    : "动态ID：-";
  const typeHint = user.last_dynamic_type
    ? user.last_dynamic_type
    : user.last_dynamic_is_video
    ? "视频"
    : "-";
  entry.dynamicType.textContent = `动态类型：${typeHint}`;
  entry.dynamicTitleStat.textContent = user.last_dynamic_title
    ? `动态标题：${user.last_dynamic_title}`
    : "动态标题：-";
  entry.dynamicTime.textContent = `动态时间：${formatTimestamp(user.last_dynamic_time)}`;
  entry.checked.textContent = `检测时间：${formatCheckedAt(user.checked_at)}`;
  const nextAt = resolveNextPollAt(user);
  entry.nextPollAt = nextAt ? nextAt.getTime() : null;
  entry.nextPoll.textContent = `刷新倒计时：${formatCountdown(entry.nextPollAt)}`;

  const textSource =
    user.last_dynamic_text || user.last_dynamic_title || "暂无动态";
  const text = truncate(textSource);
  const link = user.last_dynamic_url;
  if (link) {
    entry.dynamicText.innerHTML = "";
    const span = createElement("span", "subtext", text);
    const anchor = createElement("a", "dynamic-link", "查看动态");
    anchor.href = link;
    anchor.target = "_blank";
    anchor.rel = "noopener";
    entry.dynamicText.appendChild(span);
    entry.dynamicText.appendChild(anchor);
  } else {
    entry.dynamicText.textContent = text;
  }
}

function resolveNextPollAt(user) {
  if (user.next_poll_at) {
    const dt = new Date(user.next_poll_at);
    if (!Number.isNaN(dt.getTime())) return dt;
  }
  if (user.checked_at && user.poll_interval) {
    const checked = new Date(user.checked_at);
    if (!Number.isNaN(checked.getTime())) {
      return new Date(checked.getTime() + Number(user.poll_interval) * 1000);
    }
  }
  return null;
}

function formatCountdown(nextPollAtMs) {
  if (!nextPollAtMs) return "-";
  const diff = Math.round((nextPollAtMs - Date.now()) / 1000);
  if (diff <= 0) return "即将刷新";
  return `${diff} 秒`;
}

function updateCountdowns() {
  for (const entry of cards.values()) {
    if (!entry.nextPoll) continue;
    entry.nextPoll.textContent = `刷新倒计时：${formatCountdown(entry.nextPollAt)}`;
  }
}

function renderUsers(users) {
  if (users.length === 1) {
    container.classList.add("single");
  } else {
    container.classList.remove("single");
  }

  if (!users.length) {
    container.classList.remove("single");
    container.replaceChildren(createElement("div", "empty", "暂无 UP 主，请先登录添加。"));
    cards.clear();
    return;
  }

  const fragment = document.createDocumentFragment();
  const seen = new Set();
  users.forEach((user) => {
    const entry = ensureCard(user);
    updateCard(entry, user);
    fragment.appendChild(entry.card);
    seen.add(user.id);
  });
  container.replaceChildren(fragment);

  for (const id of cards.keys()) {
    if (!seen.has(id)) cards.delete(id);
  }

  if (!hasRendered) {
    hasRendered = true;
  } else {
    container.classList.add("no-anim");
  }
}

async function loadUsers() {
  try {
    const res = await fetch("/api/users", { cache: "no-store" });
    const data = await res.json();
    renderUsers(data);
  } catch (err) {
    console.error(err);
  }
}

loadUsers();
setInterval(loadUsers, 5000);
setInterval(updateCountdowns, 1000);
