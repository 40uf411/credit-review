// The chat page: a thin client over POST /conversations and POST /conversations/{id}/messages.
//
// - The API key lives in sessionStorage (this tab only, never in a URL); the user id, a random one per browser, in
//   localStorage, so the API's binding of a conversation to its user applies here as it does to any client.
// - Everything the service returns (answers, quotes, titles) is set with textContent, never innerHTML: an answer or
//   a quoted source cannot add markup or script to the page.
"use strict";

const MAX_CHARS = 1000;
const store = {
  get(area, key) { try { return window[area].getItem(key); } catch { return null; } },
  set(area, key, value) { try { window[area].setItem(key, value); } catch { /* storage blocked: keep in memory */ } },
  remove(area, key) { try { window[area].removeItem(key); } catch { /* ignore */ } },
};

const state = {
  key: store.get("sessionStorage", "assistant.key"),
  user: store.get("localStorage", "assistant.user"),
  conversation: store.get("sessionStorage", "assistant.conversation"),
  remaining: null,
  busy: false,
};

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function randomUser() {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return "web-" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

class HttpError extends Error {
  constructor(status) { super("HTTP " + status); this.status = status; }
}

async function call(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: { "X-API-Key": state.key, "X-User-Id": state.user, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new HttpError(response.status);
  return response.json();
}

// ------------------------------------------------------------------ views

function show(view) {
  $("unlock").hidden = view !== "unlock";
  $("chat").hidden = view !== "chat";
  for (const id of ["new", "forget"]) $(id).hidden = view !== "chat";
  $("remaining").hidden = view !== "chat" || state.remaining === null;
  if (view === "unlock") $("key").focus();
  if (view === "chat") $("question").focus();
}

function setRemaining(n) {
  state.remaining = n;
  const badge = $("remaining");
  badge.textContent = n === 1 ? "1 question left" : `${n} questions left`;
  badge.hidden = false;
  updateComposer();
}

function updateComposer() {
  const length = $("question").value.length;
  const count = $("count");
  count.textContent = `${length} / ${MAX_CHARS}`;
  count.classList.toggle("over", length > MAX_CHARS);
  const done = state.remaining === 0;
  $("question").disabled = done || state.busy;
  $("send").disabled = done || state.busy || $("question").value.trim() === "";
  for (const chip of document.querySelectorAll(".chip")) chip.disabled = done || state.busy;
  $("question").placeholder = done ? "This conversation has used its 5 questions. Start a new one."
                                   : "Ask about a mortgage-lending rule…";
}

function addUser(text) {
  const item = el("li", "msg user");
  item.append(el("div", "text", text));
  $("log").append(item);
  item.scrollIntoView({ block: "end", behavior: "smooth" });
}

function addPending() {
  const item = el("li", "msg assistant pending", "Checking the question and searching the sources…");
  $("log").append(item);
  item.scrollIntoView({ block: "end", behavior: "smooth" });
  return item;
}

function renderReply(item, reply) {
  item.className = "msg assistant" + (reply.refused ? " refused" : "");
  item.replaceChildren(el("div", "text", reply.answer));
  if (reply.refused) {
    item.append(el("span", "code", "refused: " + reply.code));
    return;
  }
  if (reply.citations && reply.citations.length) {
    const list = el("ul", "cites");
    for (const c of reply.citations) {
      const cite = el("li", "cite");
      const doc = el("div", "doc", c.title || c.source || "Source");
      doc.append(el("span", "id", c.chunk_id));
      cite.append(doc, el("blockquote", "", c.quote));
      list.append(cite);
    }
    item.append(list);
  }
}

function renderError(item, message) {
  item.className = "msg assistant refused";
  item.replaceChildren(el("div", "text", message));
}

// ------------------------------------------------------------------ actions

async function startConversation() {
  const created = await call("POST", "/conversations");
  state.conversation = created.id;
  store.set("sessionStorage", "assistant.conversation", created.id);
  $("log").replaceChildren();
  setRemaining(created.remaining_messages);
}

async function resume() {
  if (!state.conversation) return startConversation();
  try {
    const conversation = await call("GET", `/conversations/${encodeURIComponent(state.conversation)}`);
    $("log").replaceChildren();
    for (const turn of conversation.turns) {
      if (turn.role === "user") addUser(turn.text);
      else renderReply(addPending(), { answer: turn.text, refused: false, citations: [] });
    }
    setRemaining(conversation.remaining_messages);
  } catch (error) {
    if (error instanceof HttpError && error.status === 404) return startConversation();   // expired after 24h
    throw error;
  }
}

async function unlock(key) {
  state.key = key;
  try {
    await resume();
    store.set("sessionStorage", "assistant.key", key);
    $("key-error").hidden = true;
    show("chat");
  } catch (error) {
    state.key = null;
    const message = error instanceof HttpError && error.status === 401 ? "That key was not accepted."
      : "The service did not answer. Try again in a moment.";
    $("key-error").textContent = message;
    $("key-error").hidden = false;
    show("unlock");
  }
}

function forgetKey() {
  state.key = null;
  store.remove("sessionStorage", "assistant.key");
  $("key").value = "";
  show("unlock");
}

async function ask(text) {
  if (state.busy || state.remaining === 0 || !text.trim()) return;
  state.busy = true;
  $("question").value = "";
  updateComposer();
  addUser(text);
  const pending = addPending();
  try {
    const reply = await call("POST", `/conversations/${encodeURIComponent(state.conversation)}/messages`, { text });
    renderReply(pending, reply);
    setRemaining(reply.remaining_messages);
  } catch (error) {
    if (error instanceof HttpError && error.status === 401) {
      pending.remove();
      forgetKey();
      $("key-error").textContent = "The key is no longer accepted. Paste it again.";
      $("key-error").hidden = false;
    } else if (error instanceof HttpError && error.status === 404) {
      renderError(pending, "This conversation has expired. Start a new one.");
    } else {
      renderError(pending, "The service did not answer. Try again in a moment.");
    }
  } finally {
    state.busy = false;
    updateComposer();
    if (!$("chat").hidden) $("question").focus();
  }
}

// ------------------------------------------------------------------ wiring

document.addEventListener("DOMContentLoaded", () => {
  if (!state.user) {
    state.user = randomUser();
    store.set("localStorage", "assistant.user", state.user);
  }

  $("key-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const key = $("key").value.trim();
    if (key) unlock(key);
  });
  $("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    ask($("question").value);
  });
  $("question").addEventListener("input", updateComposer);
  $("question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      ask($("question").value);
    }
  });
  for (const chip of document.querySelectorAll(".chip")) {
    chip.addEventListener("click", () => ask(chip.textContent));
  }
  $("new").addEventListener("click", async () => {
    if (state.busy) return;
    try { await startConversation(); } catch { renderError(addPending(), "Could not start a new conversation."); }
    $("question").focus();
  });
  $("forget").addEventListener("click", forgetKey);

  updateComposer();
  if (state.key) unlock(state.key);
  else show("unlock");
});
