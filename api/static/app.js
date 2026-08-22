/* Lodestone front end.
 *
 * No build step and no framework: the whole surface is one filter form driving
 * one endpoint, and a toolchain would cost more than it returns here.
 *
 * Filter state lives in the URL query string, so every search is linkable and
 * the back button behaves. */

const state = {
  query: "",
  isSemantic: false,
  genres: new Map(),      // value -> "include" | "exclude"
  characters: new Map(),
  fandom: "",
  ship: "",
  ratings: new Set(),
  language: "",
  status: "",
  onlyCrossovers: false,
  excludeAbandoned: false,
  onlyAbandoned: false,
  minWords: null,
  maxWords: null,
  minFavorites: null,
  minChapters: null,
  sort: "updated",
  page: 1,
};

const elements = {
  queryInput: document.getElementById("queryInput"),
  semanticToggle: document.getElementById("semanticToggle"),
  sortSelect: document.getElementById("sortSelect"),
  searchForm: document.getElementById("searchForm"),
  storyList: document.getElementById("storyList"),
  resultCount: document.getElementById("resultCount"),
  activeFilters: document.getElementById("activeFilters"),
  corpusStats: document.getElementById("corpusStats"),
  genrePills: document.getElementById("genrePills"),
  ratingPills: document.getElementById("ratingPills"),
  characterPills: document.getElementById("characterPills"),
  characterInput: document.getElementById("characterInput"),
  characterOptions: document.getElementById("characterOptions"),
  fandomSelect: document.getElementById("fandomSelect"),
  shipSelect: document.getElementById("shipSelect"),
  languageSelect: document.getElementById("languageSelect"),
  statusPills: document.getElementById("statusPills"),
  onlyCrossovers: document.getElementById("onlyCrossovers"),
  excludeAbandoned: document.getElementById("excludeAbandoned"),
  onlyAbandoned: document.getElementById("onlyAbandoned"),
  minWords: document.getElementById("minWords"),
  maxWords: document.getElementById("maxWords"),
  minFavorites: document.getElementById("minFavorites"),
  minChapters: document.getElementById("minChapters"),
  resetFilters: document.getElementById("resetFilters"),
  pager: document.getElementById("pager"),
  prevPage: document.getElementById("prevPage"),
  nextPage: document.getElementById("nextPage"),
  pageLabel: document.getElementById("pageLabel"),
};

const PAGE_SIZE = 25;
const formatNumber = (value) => (value ?? 0).toLocaleString("en-GB");

/* ---------------------------------------------------------------- helpers */

function cycleTriState(currentState) {
  // off -> include -> exclude -> off. Exclusion is the feature FFN lacks
  // entirely, so it deserves to be one click away rather than buried.
  if (!currentState) return "include";
  if (currentState === "include") return "exclude";
  return null;
}

function buildSearchParams() {
  const params = new URLSearchParams();
  // The same box drives both modes: "by meaning" sends the text to be embedded
  // and ranked by vector distance, otherwise it is a literal keyword match.
  if (state.query) params.set(state.isSemantic ? "semantic" : "q", state.query);
  if (state.fandom) params.append("fandom", state.fandom);
  if (state.language) params.append("language", state.language);
  if (state.status) params.set("status", state.status);

  for (const rating of state.ratings) params.append("rating", rating);

  for (const [value, mode] of state.genres) {
    params.append(mode === "exclude" ? "excludeGenre" : "genre", value);
  }
  for (const [value, mode] of state.characters) {
    params.append(mode === "exclude" ? "excludeCharacter" : "character", value);
  }

  // A ship arrives as "A / B" and is sent as its individual members; the API
  // requires them to share one bracket group.
  if (state.ship) {
    for (const member of state.ship.split(" / ")) params.append("ship", member);
  }

  // Crossovers live in no parent fandom archive on FFN, so they are
  // effectively unfindable there. Here they are just another filter.
  if (state.onlyCrossovers) params.set("crossover", "true");
  if (state.excludeAbandoned) params.set("excludeAbandoned", "true");
  if (state.onlyAbandoned) params.set("onlyAbandoned", "true");

  for (const key of ["minWords", "maxWords", "minFavorites", "minChapters"]) {
    if (state[key] !== null && state[key] !== "") params.set(key, state[key]);
  }

  params.set("sort", state.sort);
  params.set("page", state.page);
  params.set("pageSize", PAGE_SIZE);
  return params;
}

function syncUrl() {
  const params = buildSearchParams();
  params.delete("pageSize");
  const queryString = params.toString();
  history.replaceState(null, "", queryString ? `?${queryString}` : location.pathname);
}

function restoreFromUrl() {
  const params = new URLSearchParams(location.search);
  state.query = params.get("q") || params.get("semantic") || "";
  state.isSemantic = params.has("semantic");
  state.fandom = params.get("fandom") || "";
  state.language = params.get("language") || "";
  state.status = params.get("status") || "";
  state.sort = params.get("sort") || "updated";
  state.page = Number(params.get("page")) || 1;
  state.onlyCrossovers = params.get("crossover") === "true";
  state.excludeAbandoned = params.get("excludeAbandoned") === "true";
  state.onlyAbandoned = params.get("onlyAbandoned") === "true";

  for (const rating of params.getAll("rating")) state.ratings.add(rating);
  for (const genre of params.getAll("genre")) state.genres.set(genre, "include");
  for (const genre of params.getAll("excludeGenre")) state.genres.set(genre, "exclude");
  for (const name of params.getAll("character")) state.characters.set(name, "include");
  for (const name of params.getAll("excludeCharacter")) state.characters.set(name, "exclude");

  const shipMembers = params.getAll("ship");
  if (shipMembers.length) state.ship = [...shipMembers].sort().join(" / ");

  for (const key of ["minWords", "maxWords", "minFavorites", "minChapters"]) {
    const value = params.get(key);
    if (value) state[key] = Number(value);
  }

  elements.queryInput.value = state.query;
  elements.semanticToggle.checked = state.isSemantic;
  elements.sortSelect.value = state.sort;
  elements.onlyCrossovers.checked = state.onlyCrossovers;
  elements.excludeAbandoned.checked = state.excludeAbandoned;
  elements.onlyAbandoned.checked = state.onlyAbandoned;
  for (const key of ["minWords", "maxWords", "minFavorites", "minChapters"]) {
    if (state[key] !== null) elements[key].value = state[key];
  }
}

/* ------------------------------------------------------------- rendering */

function renderPillGroup(container, entries, stateMap, onChange) {
  container.replaceChildren();
  for (const entry of entries) {
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "pill";
    pill.textContent = entry.value;

    if (entry.count !== undefined) {
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = formatNumber(entry.count);
      pill.appendChild(count);
    }

    const currentState = stateMap.get(entry.value);
    if (currentState) pill.dataset.state = currentState;

    pill.addEventListener("click", () => {
      const nextState = cycleTriState(stateMap.get(entry.value));
      if (nextState) {
        stateMap.set(entry.value, nextState);
        pill.dataset.state = nextState;
      } else {
        stateMap.delete(entry.value);
        delete pill.dataset.state;
      }
      onChange();
    });
    container.appendChild(pill);
  }
}

function renderCharacterPills() {
  elements.characterPills.replaceChildren();
  for (const [name, mode] of state.characters) {
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "pill";
    pill.dataset.state = mode;
    pill.textContent = `${name} ×`;
    pill.title = mode === "exclude" ? "Excluded — click to remove" : "Click to exclude";
    pill.addEventListener("click", () => {
      if (mode === "include") state.characters.set(name, "exclude");
      else state.characters.delete(name);
      renderCharacterPills();
      runSearch(true);
    });
    elements.characterPills.appendChild(pill);
  }
}

function renderActiveFilters() {
  const chips = [];
  if (state.fandom) chips.push(["Fandom: " + state.fandom, () => { state.fandom = ""; elements.fandomSelect.value = ""; }]);
  if (state.ship) chips.push(["Ship: " + state.ship, () => { state.ship = ""; elements.shipSelect.value = ""; }]);
  if (state.status) chips.push(["Status: " + state.status.replace("_", " "), () => { state.status = ""; syncStatusPills(); }]);
  if (state.language) chips.push(["Lang: " + state.language, () => { state.language = ""; elements.languageSelect.value = ""; }]);
  if (state.onlyCrossovers) chips.push(["Crossovers only", () => { state.onlyCrossovers = false; elements.onlyCrossovers.checked = false; }]);
  if (state.onlyAbandoned) chips.push(["Only abandoned", () => { state.onlyAbandoned = false; elements.onlyAbandoned.checked = false; }]);
  if (state.excludeAbandoned) chips.push(["Hiding abandoned", () => { state.excludeAbandoned = false; elements.excludeAbandoned.checked = false; }]);

  elements.activeFilters.replaceChildren();
  for (const [label, clear] of chips) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "pill";
    chip.dataset.state = "include";
    chip.textContent = `${label} ×`;
    chip.addEventListener("click", () => { clear(); runSearch(true); });
    elements.activeFilters.appendChild(chip);
  }
}

function renderStory(story) {
  const item = document.createElement("li");
  item.className = "story";

  const title = document.createElement("h3");
  title.className = "story-title";
  const link = document.createElement("a");
  link.href = story.url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = story.title;
  title.appendChild(link);

  const byline = document.createElement("p");
  byline.className = "byline";
  byline.append("by ");
  const author = document.createElement("span");
  author.className = "author";
  author.textContent = story.author_name;
  byline.appendChild(author);

  const summary = document.createElement("p");
  summary.className = "summary";
  summary.textContent = story.summary;

  const badges = document.createElement("div");
  badges.className = "badges";

  const addBadge = (text, className = "") => {
    const badge = document.createElement("span");
    badge.className = `badge ${className}`.trim();
    badge.textContent = text;
    badges.appendChild(badge);
  };

  for (const fandom of story.fandoms || []) addBadge(fandom, "fandom");
  if (story.is_crossover) addBadge("Crossover", "crossover");
  if (story.rating) addBadge(story.rating, "rating");
  for (const genre of story.genres || []) addBadge(genre);
  for (const ship of story.ships || []) addBadge(ship.join(" / "), "ship");
  if (story.status === "complete") addBadge("Complete", "complete");
  if (story.is_abandoned) addBadge("Abandoned", "abandoned");
  if (story.language && story.language !== "English") addBadge(story.language);

  const figures = document.createElement("div");
  figures.className = "figures";
  const figureParts = [
    `${formatNumber(story.word_count)} words`,
    `${formatNumber(story.chapter_count)} ch`,
    `${formatNumber(story.favorite_count)} favs`,
    `${formatNumber(story.review_count)} reviews`,
    `updated ${(story.updated_at || "").slice(0, 10)}`,
  ];
  for (const part of figureParts) {
    const span = document.createElement("span");
    span.textContent = part;
    figures.appendChild(span);
  }
  if (story.favorites_per_1k_words) {
    const ratio = document.createElement("span");
    ratio.className = "ratio";
    ratio.textContent = `${Number(story.favorites_per_1k_words).toFixed(1)} favs/1K`;
    ratio.title = "Favourites per thousand words — popularity adjusted for length";
    figures.appendChild(ratio);
  }

  item.append(title, byline, summary, badges, figures);
  return item;
}

/* --------------------------------------------------------------- fetching */

async function runSearch(resetPage = false) {
  if (resetPage) state.page = 1;
  syncUrl();

  document.querySelector(".degraded-notice")?.remove();
  elements.resultCount.textContent = "Searching…";
  const response = await fetch(`/api/search?${buildSearchParams()}`);
  if (!response.ok) {
    elements.resultCount.textContent = "Search failed.";
    return;
  }
  const payload = await response.json();

  elements.storyList.replaceChildren();
  if (payload.results.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.innerHTML = "<h3>Nothing matches</h3><p>Try removing an exclusion or widening the word count.</p>";
    elements.storyList.appendChild(empty);
  } else {
    for (const story of payload.results) elements.storyList.appendChild(renderStory(story));
  }

  elements.resultCount.innerHTML = `<strong>${formatNumber(payload.total)}</strong> stories`;
  if (state.isSemantic && state.query && !payload.semantic) {
    // The API degrades to keyword search when the embedding server is down.
    // Say so rather than passing off keyword results as semantic ones.
    const notice = document.createElement("p");
    notice.className = "degraded-notice";
    notice.textContent = "Meaning search unavailable — showing keyword matches instead.";
    elements.resultCount.after(notice);
  }
  renderActiveFilters();

  const lastPage = Math.max(1, Math.ceil(payload.total / PAGE_SIZE));
  elements.pager.hidden = payload.total <= PAGE_SIZE;
  elements.pageLabel.textContent = `Page ${state.page} of ${formatNumber(lastPage)}`;
  elements.prevPage.disabled = state.page <= 1;
  elements.nextPage.disabled = state.page >= lastPage;
}

async function loadFacets() {
  const [facets, stats] = await Promise.all([
    fetch("/api/facets").then((r) => r.json()),
    fetch("/api/stats").then((r) => r.json()),
  ]);

  renderPillGroup(elements.genrePills, facets.genres, state.genres, () => runSearch(true));

  // Ratings are include-only: excluding a rating is the same as selecting the
  // others, so a tri-state pill here would be a confusing way to say nothing new.
  elements.ratingPills.replaceChildren();
  for (const entry of facets.ratings) {
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "pill";
    pill.textContent = entry.value;
    if (state.ratings.has(entry.value)) pill.dataset.state = "include";
    pill.addEventListener("click", () => {
      if (state.ratings.has(entry.value)) {
        state.ratings.delete(entry.value);
        delete pill.dataset.state;
      } else {
        state.ratings.add(entry.value);
        pill.dataset.state = "include";
      }
      runSearch(true);
    });
    elements.ratingPills.appendChild(pill);
  }

  const fillSelect = (select, entries, labelFor) => {
    for (const entry of entries) {
      const option = document.createElement("option");
      option.value = entry.value;
      option.textContent = labelFor(entry);
      select.appendChild(option);
    }
  };
  fillSelect(elements.fandomSelect, facets.fandoms, (e) => `${e.value} (${formatNumber(e.count)})`);
  fillSelect(elements.languageSelect, facets.languages, (e) => `${e.value} (${formatNumber(e.count)})`);
  fillSelect(elements.shipSelect, facets.ships, (e) => `${e.value} (${formatNumber(e.count)})`);

  elements.fandomSelect.value = state.fandom;
  elements.languageSelect.value = state.language;
  elements.shipSelect.value = state.ship;

  for (const entry of facets.characters) {
    const option = document.createElement("option");
    option.value = entry.value;
    elements.characterOptions.appendChild(option);
  }

  elements.corpusStats.replaceChildren();
  const statFields = [
    ["Stories", formatNumber(stats.stories)],
    ["Authors", formatNumber(stats.authors)],
    ["Complete", formatNumber(stats.complete)],
    ["Abandoned", formatNumber(stats.abandoned)],
  ];
  for (const [label, value] of statFields) {
    const group = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = label;
    const definition = document.createElement("dd");
    definition.textContent = value;
    group.append(term, definition);
    elements.corpusStats.appendChild(group);
  }
}

/* ----------------------------------------------------------------- events */

function syncStatusPills() {
  for (const pill of elements.statusPills.querySelectorAll(".pill")) {
    if (pill.dataset.status === state.status) pill.dataset.state = "include";
    else delete pill.dataset.state;
  }
}

elements.searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  state.query = elements.queryInput.value.trim();
  runSearch(true);
});

elements.semanticToggle.addEventListener("change", () => {
  state.isSemantic = elements.semanticToggle.checked;
  // Ranking by meaning is the whole point of the mode, so switch to it; going
  // back restores recency rather than leaving a dead sort selected.
  state.sort = state.isSemantic ? "semantic" : "updated";
  elements.sortSelect.value = state.sort;
  if (state.query) runSearch(true);
});

elements.sortSelect.addEventListener("change", () => {
  state.sort = elements.sortSelect.value;
  runSearch(true);
});

for (const pill of elements.statusPills.querySelectorAll(".pill")) {
  pill.addEventListener("click", () => {
    state.status = state.status === pill.dataset.status ? "" : pill.dataset.status;
    syncStatusPills();
    runSearch(true);
  });
}

elements.characterInput.addEventListener("change", () => {
  const name = elements.characterInput.value.trim();
  if (!name) return;
  state.characters.set(name, "include");
  elements.characterInput.value = "";
  renderCharacterPills();
  runSearch(true);
});

for (const [element, key] of [
  [elements.fandomSelect, "fandom"],
  [elements.languageSelect, "language"],
  [elements.shipSelect, "ship"],
]) {
  element.addEventListener("change", () => { state[key] = element.value; runSearch(true); });
}

for (const [element, key] of [
  [elements.onlyCrossovers, "onlyCrossovers"],
  [elements.excludeAbandoned, "excludeAbandoned"],
  [elements.onlyAbandoned, "onlyAbandoned"],
]) {
  element.addEventListener("change", () => { state[key] = element.checked; runSearch(true); });
}

for (const key of ["minWords", "maxWords", "minFavorites", "minChapters"]) {
  elements[key].addEventListener("change", () => {
    const value = elements[key].value;
    state[key] = value === "" ? null : Number(value);
    runSearch(true);
  });
}

elements.prevPage.addEventListener("click", () => { state.page -= 1; runSearch(); window.scrollTo(0, 0); });
elements.nextPage.addEventListener("click", () => { state.page += 1; runSearch(); window.scrollTo(0, 0); });

elements.resetFilters.addEventListener("click", () => {
  location.href = location.pathname;
});

/* -------------------------------------------------------------- bootstrap */

restoreFromUrl();
syncStatusPills();
renderCharacterPills();
loadFacets().then(() => runSearch());
