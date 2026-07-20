// ══════════════════════════════════════════
// 椿汐岛 Service Worker
// PWA 推送通知 + 离线缓存
// ══════════════════════════════════════════

const CACHE_NAME = 'tsubakishima-v1';

// ── 安装：预缓存核心资源 ──
self.addEventListener('install', (event) => {
  console.log('[SW] 安装中...');
  self.skipWaiting();
});

// ── 激活：清理旧缓存 ──
self.addEventListener('activate', (event) => {
  console.log('[SW] 已激活');
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// ── 推送事件：收到后端推送时触发 ──
self.addEventListener('push', (event) => {
  console.log('[SW] 收到推送:', event);

  let data = {};
  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      data = { title: '新消息', body: event.data.text() };
    }
  }

  const title = data.title || '椿汐岛';
  const options = {
    body: data.body || '',
    icon: data.icon || '/icon-192.png',
    badge: '/icon-192.png',
    tag: data.tag || 'tsubaki-msg',
    requireInteraction: data.requireInteraction || false,
    vibrate: data.vibrate || [200, 100, 200],
    timestamp: data.timestamp || Date.now(),
    data: {
      url: data.url || '/',
      charId: data.charId || null
    }
  };

  event.waitUntil(
    self.registration.showNotification(title, options)
  );
});

// ── 通知点击：打开应用并跳转到聊天 ──
self.addEventListener('notificationclick', (event) => {
  console.log('[SW] 通知被点击:', event);
  event.notification.close();

  const urlToOpen = event.notification.data?.url || '/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      // 如果已有打开的窗口，聚焦它
      for (const client of windowClients) {
        if (client.url.includes(urlToOpen) || client.url === '/' || client.url.endsWith('/index.html')) {
          client.focus();
          // 发送消息给页面，告知点击了通知
          client.postMessage({
            type: 'notification-click',
            charId: event.notification.data?.charId
          });
          return;
        }
      }
      // 没有打开的窗口，打开新窗口
      if (clients.openWindow) {
        return clients.openWindow(urlToOpen);
      }
    })
  );
});

// ── 消息传递：页面与 SW 通信 ──
self.addEventListener('message', (event) => {
  console.log('[SW] 收到页面消息:', event.data);
});