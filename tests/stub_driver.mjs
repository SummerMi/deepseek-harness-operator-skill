// Lightweight ego-browser API stub used by tests/test_operator.py.
//
// It implements only the documented ego-browser v2.1.0 surface the generated
// connect module is allowed to use:
//   listTaskSpaces(), taskSpace(idOrName), claimTaskSpace(id)
//   space.pages() / space.tabs() / space.newPage() / space.adopt(page)
//   space.page(label), space.spaceId, space.name, space.ownership
//   page.label, page.url() (async), page.goto(), page.snapshot()
// Tabs are { label?, page, targetId, title, url, active, openedBy }.
//
// Usage: node stub_driver.mjs <file-url-of-module> <mode>

const [moduleUrl, mode] = process.argv.slice(2);
const log = [];
const ORIGIN = 'http://127.0.0.1:5555';
const AUTH = ORIGIN + '/?token=SYNTHETIC#frag';

function logCall(...entry) {
  log.push(entry);
}

function originOf(value) {
  try {
    return new URL(String(value)).origin;
  } catch {
    return null;
  }
}

function makePage(label, startUrl) {
  const page = {
    label,
    spaceId: 3,
    openedBy: 'agent',
    targetId: 'target-' + String(label || 'unmanaged'),
    url: async () => page.__url,
    goto: async (target) => {
      logCall('goto', page.label, originOf(target));
      page.__url = target;
    },
    snapshot: async () => 'SNAPSHOT-' + String(page.label),
    close: async () => {},
  };
  page.__url = startUrl;
  return page;
}

function makeTab(page, url, extra = {}) {
  return {
    label: page.label,
    page,
    targetId: page.targetId,
    title: extra.title || '',
    url,
    active: extra.active !== false,
    openedBy: extra.openedBy || 'agent',
  };
}

function makeSpace(options = {}) {
  const id = options.id === undefined ? 3 : options.id;
  const name = options.name || 'DeepSeek Harness Operator';
  const pages = options.pages || [];
  const tabs = options.tabs || [];
  return {
    spaceId: id,
    name,
    ownership: options.ownership || 'agent',
    pages: async () => {
      logCall('pages', id);
      return pages;
    },
    tabs: async () => {
      logCall('tabs', id);
      return tabs;
    },
    newPage: async () => {
      logCall('newPage', id);
      const page = makePage('p' + (pages.length + 1), 'about:blank');
      pages.push(page);
      tabs.push(makeTab(page, 'about:blank'));
      return page;
    },
    adopt: async (page) => {
      logCall('adopt', page ? page.targetId : null);
      page.label = 'p9';
      pages.push(page);
      const tab = tabs.find((item) => item.page === page);
      if (tab) tab.label = 'p9';
      return page;
    },
    page: (label) => {
      logCall('page', label);
      return pages.find((item) => item.label === label);
    },
  };
}

function freshSpace(name) {
  const p1 = makePage('p1', 'about:blank');
  return makeSpace({
    name,
    pages: [p1],
    tabs: [makeTab(p1, 'about:blank', { title: 'New tab' })],
  });
}

switch (mode) {
  case 'create': {
    globalThis.taskSpace = async (name) => {
      logCall('taskSpace', name);
      return freshSpace(name);
    };
    break;
  }
  case 'reuse': {
    const p2 = makePage('p2', ORIGIN);
    const space = makeSpace({ pages: [p2], tabs: [makeTab(p2, ORIGIN)] });
    globalThis.taskSpace = async (name) => {
      logCall('taskSpace', name);
      return space;
    };
    break;
  }
  case 'claim': {
    globalThis.listTaskSpaces = async () => {
      logCall('listTaskSpaces');
      return [{ id: 7, name: 'Harness operator 20261029', ownership: 'user' }];
    };
    globalThis.claimTaskSpace = async (id) => {
      logCall('claimTaskSpace', id);
      const owned = makePage(undefined, ORIGIN);
      return makeSpace({
        id,
        name: 'Harness operator 20261029',
        ownership: 'agent',
        pages: [],
        tabs: [makeTab(owned, ORIGIN, { openedBy: 'user' })],
      });
    };
    break;
  }
  case 'unrelated': {
    const p1 = makePage('p1', 'about:blank');
    const stranger = makePage(undefined, 'http://127.0.0.1:9999/other');
    const space = makeSpace({
      pages: [p1],
      tabs: [
        makeTab(p1, 'about:blank', { title: 'New tab' }),
        makeTab(stranger, 'http://127.0.0.1:9999/other', { openedBy: 'user' }),
      ],
    });
    globalThis.taskSpace = async (name) => {
      logCall('taskSpace', name);
      return space;
    };
    break;
  }
  case 'managed-unrelated': {
    const busy = makePage('p1', 'http://127.0.0.1:9999/other');
    const space = makeSpace({ pages: [busy], tabs: [makeTab(busy, busy.__url)] });
    globalThis.taskSpace = async () => space;
    break;
  }
  case 'ambiguous': {
    const a = makePage('p1', ORIGIN);
    const b = makePage('p2', ORIGIN);
    const space = makeSpace({ pages: [a, b], tabs: [makeTab(a, ORIGIN), makeTab(b, ORIGIN)] });
    globalThis.taskSpace = async (name) => {
      logCall('taskSpace', name);
      return space;
    };
    break;
  }
  case 'name-in-use': {
    globalThis.taskSpace = async (name) => {
      logCall('taskSpace', name);
      throw new Error('EGO_TASK_SPACE_NAME_IN_USE: the name belongs to a user-owned space');
    };
    globalThis.claimTaskSpace = async (id) => {
      logCall('claimTaskSpace', id);
      throw new Error('must not be called automatically');
    };
    break;
  }
  default:
    console.error('unknown mode ' + mode);
    process.exitCode = 64;
}

try {
  await import(moduleUrl);
} finally {
  console.error('__STUB_LOG__' + JSON.stringify(log));
}
