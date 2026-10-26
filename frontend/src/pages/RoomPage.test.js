import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import RoomPage from './RoomPage.vue';

const mocks = vi.hoisted(() => ({
  userId: { value: 'owner' },
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  handlers: new Map(),
  showToast: vi.fn(),
  isGuest: null,
  guestRequestAvailableAt: null,
  serverClockOffset: null,
  fetchMe: vi.fn(),
}));

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
  onBeforeRouteLeave: vi.fn(),
}));
vi.mock('../components/ThemeToggle.vue', () => ({ default: { template: '<span />' } }));
vi.mock('qrcode', () => ({ default: { toCanvas: vi.fn().mockResolvedValue() } }));
vi.mock('../composables/useAuth.js', async () => {
  const { ref } = await import('vue');
  mocks.isGuest = ref(false);
  mocks.guestRequestAvailableAt = ref(null);
  mocks.serverClockOffset = ref(0);
  return { useAuth: () => ({ userId: mocks.userId, isGuest: mocks.isGuest, guestRequestAvailableAt: mocks.guestRequestAvailableAt, serverClockOffset: mocks.serverClockOffset, fetchMe: mocks.fetchMe }) };
});
vi.mock('../composables/useApi.js', () => ({
  apiGet: mocks.apiGet,
  apiPost: mocks.apiPost,
  apiPut: mocks.apiPut,
  apiPatch: mocks.apiPatch,
  apiDelete: mocks.apiDelete,
}));
vi.mock('../composables/useToast.js', () => ({
  useToast: () => ({ showToast: mocks.showToast }),
}));
vi.mock('../composables/useWebSocket.js', () => ({
  useWebSocket: () => ({
    connectToRoom: vi.fn(),
    disconnect: vi.fn(),
    onEvent: (name, handler) => mocks.handlers.set(name, handler),
    offEvent: (name) => mocks.handlers.delete(name),
  }),
}));

let wrapper;
let room;
let pending;
let authorized;
let devices;
let selectedDeviceId;
let admissions;
let rateLimitUntil;

beforeEach(() => {
  vi.clearAllMocks();
  mocks.handlers.clear();
  mocks.userId.value = 'owner';
  mocks.isGuest.value = false;
  mocks.guestRequestAvailableAt.value = null;
  mocks.serverClockOffset.value = 0;
  mocks.fetchMe.mockResolvedValue({});
  room = { host_user_id: 'owner', admin_user_ids: ['admin'], room_name: 'Music room', room_code: 'ABC' };
  pending = [];
  authorized = true;
  devices = [{ id: 'desktop', name: 'Desktop', type: 'computer', is_active: true, is_restricted: false }];
  selectedDeviceId = 'desktop';
  admissions = [];
  rateLimitUntil = null;
  mocks.apiGet.mockImplementation(async (path) => {
    if (path === '/playback/spotify/status') return { authorized, rate_limit_until: rateLimitUntil };
    if (path === '/playback/devices') return { devices, selected_device_id: selectedDeviceId };
    if (path === '/platform-connections/spotify/has-app-credentials') return { has_credentials: true };
    if (path === '/rooms/room') return room;
    if (path === '/rooms/room/admissions') return admissions;
    if (path === '/session/by-room/room') return { id: 'session' };
    if (path === '/rooms/room/playback') return { status: 'stopped', rate_limit_until: rateLimitUntil };
    if (path === '/queue/current?session_id=session') return null;
    if (path === '/queue/?session_id=session') return pending;
    if (path === '/rooms/room/members') {
      return ['owner', 'admin', 'guest'].map((id) => ({ id, username: id }));
    }
    return [];
  });
});

afterEach(() => { wrapper?.unmount(); vi.useRealTimers(); });

async function renderRoom() {
  wrapper = mount(RoomPage, {
    props: { id: 'room' },
    global: { stubs: { ThemeToggle: true, RouterLink: true } },
  });
  await flushPromises();
  return wrapper;
}

describe('LAN invite links', () => {
  const url = 'http://192.168.1.165:3323/invite?token=server-generated-token';
  let clipboardDescriptor;
  let execDescriptor;

  beforeEach(() => {
    clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
    execDescriptor = Object.getOwnPropertyDescriptor(document, 'execCommand');
    mocks.apiPost.mockResolvedValue({ url });
  });

  afterEach(() => {
    if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor);
    else delete navigator.clipboard;
    if (execDescriptor) Object.defineProperty(document, 'execCommand', execDescriptor);
    else delete document.execCommand;
  });

  async function openInvite() {
    await renderRoom();
    document.body.appendChild(wrapper.element);
    await wrapper.get('[title="Show invite QR"]').trigger('click');
    await flushPromises();
    expect(wrapper.get('.qr-link').text()).toBe(url);
    const button = wrapper.findAll('button').find((item) => item.text() === 'Copy link');
    button.element.focus();
    return button;
  }

  it('copies the exact server sharing URL on HTTP without Clipboard API', async () => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
    const copy = vi.fn(() => {
      expect(document.querySelector('textarea').value).toBe(url);
      return true;
    });
    Object.defineProperty(document, 'execCommand', { value: copy, configurable: true });
    const button = await openInvite();
    await button.trigger('click');
    expect(copy).toHaveBeenCalledExactlyOnceWith('copy');
    expect(mocks.showToast).toHaveBeenCalledWith('Link copied');
    expect(document.querySelector('textarea')).toBeNull();
    expect(document.activeElement).toBe(button.element);
  });

  it('falls back after Clipboard API access is denied', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('Denied'));
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    Object.defineProperty(document, 'execCommand', { value: vi.fn().mockReturnValue(true), configurable: true });
    await (await openInvite()).trigger('click');
    await flushPromises();
    expect(writeText).toHaveBeenCalledExactlyOnceWith(url);
    expect(mocks.showToast).toHaveBeenCalledWith('Link copied');
    expect(document.querySelector('textarea')).toBeNull();
  });

  it('does not report success when both clipboard methods are denied', async () => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
    Object.defineProperty(document, 'execCommand', { value: vi.fn().mockReturnValue(false), configurable: true });
    await (await openInvite()).trigger('click');
    expect(mocks.showToast).not.toHaveBeenCalledWith('Link copied');
    expect(mocks.showToast).toHaveBeenCalledWith('Could not copy link. Select the link and copy it manually.');
    expect(wrapper.get('.qr-link').text()).toBe(url);
    expect(document.querySelector('textarea')).toBeNull();
  });
});

describe('guest song requests', () => {
  async function renderGuest(availableAfterSeconds = 300) {
    mocks.isGuest.value = true;
    mocks.userId.value = 'guest';
    mocks.guestRequestAvailableAt.value = new Date(Date.now() + availableAfterSeconds * 1000).toISOString();
    await renderRoom();
    await wrapper.get('input[placeholder="Search for songs..."]').setValue('Honey');
    await vi.advanceTimersByTimeAsync(300);
    await flushPromises();
  }

  beforeEach(() => {
    vi.useFakeTimers();
    const defaultGet = mocks.apiGet.getMockImplementation();
    mocks.apiGet.mockImplementation(async (path) => {
      if (path.startsWith('/search/?')) return [{ external_id: 'honey', title: 'Honey', artist: 'Artist' }];
      if (path === '/songs/by-external/honey?platform=spotify') return { id: 'song' };
      return defaultGet(path);
    });
  });

  it('shows the first-entry countdown and enables a request only when five minutes have elapsed', async () => {
    await renderGuest();
    expect(wrapper.get('.guest-request-notice').text()).toContain('5:00');
    expect(wrapper.get('[aria-label="Add Honey to queue"]').attributes('disabled')).toBeDefined();
    expect(mocks.fetchMe).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(300000);
    expect(wrapper.get('.guest-request-notice').text()).toContain('You can request a song now');
    expect(wrapper.get('[aria-label="Add Honey to queue"]').attributes('disabled')).toBeUndefined();
  });

  it('refreshes the persisted quota after a successful request and shows the thirty-minute wait', async () => {
    await renderGuest(-1);
    mocks.apiPost.mockImplementation(async () => {
      mocks.guestRequestAvailableAt.value = new Date(Date.now() + 1800000).toISOString();
      return {};
    });
    await wrapper.get('[aria-label="Add Honey to queue"]').trigger('click');
    await flushPromises();
    expect(mocks.apiPost).toHaveBeenCalledWith('/queue/', { session_id: 'session', song_id: 'song', group: 'manual' });
    expect(mocks.fetchMe).toHaveBeenCalledTimes(2);
    expect(wrapper.get('.guest-request-notice').text()).toContain('30:00');
    expect(wrapper.get('[aria-label="Add Honey to queue"]').attributes('disabled')).toBeDefined();
  });

  it('keeps a second request blocked while the guest already has one pending song', async () => {
    pending = [{ id: 'pending', song: { title: 'Waiting' }, added_by: { id: 'guest', username: 'Guest' } }];
    await renderGuest(-1);
    expect(wrapper.get('.guest-request-notice').text()).toContain('Your song is already in the queue');
    expect(wrapper.get('[aria-label="Add Honey to queue"]').attributes('disabled')).toBeDefined();
    mocks.handlers.get('queue_updated')({ queue: [] });
    await flushPromises();
    expect(wrapper.get('[aria-label="Add Honey to queue"]').attributes('disabled')).toBeUndefined();
  });

  it('honors a server retry deadline when a stale tab attempts a request', async () => {
    await renderGuest(-1);
    mocks.apiPost.mockRejectedValue({ status: 429, retryAfter: 120, detail: 'Guest requests are limited' });
    await wrapper.get('[aria-label="Add Honey to queue"]').trigger('click');
    await flushPromises();
    expect(wrapper.get('.guest-request-notice').text()).toContain('2:00');
    expect(wrapper.get('[aria-label="Add Honey to queue"]').attributes('disabled')).toBeDefined();
    expect(mocks.showToast).toHaveBeenCalledWith('Guest requests are limited');
  });

  it('uses the server clock offset so a wrong device clock cannot skip the wait in the UI', async () => {
    mocks.serverClockOffset.value = 3600000;
    await renderGuest(3900);
    expect(wrapper.get('.guest-request-notice').text()).toContain('5:00');
  });
});

describe('room administrator controls', () => {
  it('lets managers approve pending members and refreshes requests from targeted events', async () => {
    mocks.userId.value = 'admin';
    admissions = [{ user_id: 'new-member', username: 'New member', status: 'pending', public_id: 'CR-4827-9163' }];
    await renderRoom();
    expect(wrapper.find('[aria-label="Approve New member"]').exists()).toBe(true);
    expect(wrapper.get('.admissions-panel .user-identity-value').text()).toBe('CR-4827-9163');
    expect(wrapper.find('[aria-label="Copy CrowdRoom ID CR-4827-9163"]').exists()).toBe(true);
    mocks.apiPatch.mockImplementation(async () => { admissions = []; });
    await wrapper.get('[aria-label="Approve New member"]').trigger('click');
    await flushPromises();
    expect(mocks.apiPatch).toHaveBeenCalledWith('/rooms/room/admissions/new-member', { approved: true });
    expect(wrapper.find('[aria-label="Approve New member"]').exists()).toBe(false);
    admissions = [{ user_id: 'other', username: 'Other member', status: 'pending' }];
    await mocks.handlers.get('admission_requested')({});
    await flushPromises();
    expect(wrapper.find('[aria-label="Deny Other member"]').exists()).toBe(true);
  });

  it('keeps admission requests private from ordinary members', async () => {
    mocks.userId.value = 'guest';
    await renderRoom();
    expect(mocks.apiGet).not.toHaveBeenCalledWith('/rooms/room/admissions');
    expect(wrapper.find('.admissions-panel').exists()).toBe(false);
  });

  it('lets managers recover an accidentally declined request without denying it twice', async () => {
    mocks.userId.value = 'admin';
    admissions = [{ user_id: 'declined', username: 'Declined member', status: 'rejected' }];
    await renderRoom();
    expect(wrapper.get('.admissions-panel .badge').text()).toBe('0');
    expect(wrapper.get('.admissions-panel .members-list').text()).toContain('Declined');
    expect(wrapper.find('[aria-label="Deny Declined member"]').exists()).toBe(false);
    mocks.apiPatch.mockImplementation(async () => { admissions = []; });
    await wrapper.get('[aria-label="Approve Declined member"]').trigger('click');
    await flushPromises();
    expect(mocks.apiPatch).toHaveBeenCalledWith('/rooms/room/admissions/declined', { approved: true });
    expect(wrapper.find('[aria-label="Approve Declined member"]').exists()).toBe(false);
  });

  it('refreshes a stale request if another manager already approved the account', async () => {
    admissions = [{ user_id: 'new-member', username: 'New member', status: 'pending' }];
    await renderRoom();
    mocks.apiPatch.mockImplementation(async () => {
      admissions = [];
      throw { status: 403, detail: 'This account is already approved. Use Ban to block room access.' };
    });
    await wrapper.get('[aria-label="Deny New member"]').trigger('click');
    await flushPromises();
    expect(mocks.showToast).toHaveBeenCalledWith('This account is already approved. Use Ban to block room access.');
    expect(wrapper.find('[aria-label="Deny New member"]').exists()).toBe(false);
  });

  it('lets only the owner promote and revoke administrators', async () => {
    await renderRoom();
    mocks.apiPatch.mockResolvedValue({ ...room, admin_user_ids: ['admin', 'guest'] });
    await wrapper.get('[aria-label="Make admin: guest"]').trigger('click');
    await flushPromises();
    expect(mocks.apiPatch).toHaveBeenCalledWith('/rooms/room/admins/guest', { is_admin: true });
    expect(wrapper.find('[aria-label="Revoke admin from guest"]').exists()).toBe(true);
  });

  it('limits administrator moderation controls to ordinary members', async () => {
    mocks.userId.value = 'admin';
    await renderRoom();
    expect(wrapper.find('[aria-label="Kick guest"]').exists()).toBe(true);
    expect(wrapper.find('[aria-label="Kick owner"]').exists()).toBe(false);
    expect(wrapper.find('[aria-label="Kick admin"]').exists()).toBe(false);
    expect(wrapper.find('[title="Make admin"]').exists()).toBe(false);
    expect(wrapper.find('.members-panel .ph-prohibit').exists()).toBe(true);
  });

  it('updates controls immediately when an administrator is revoked', async () => {
    mocks.userId.value = 'admin';
    await renderRoom();
    mocks.handlers.get('admins_updated')({ payload: { admin_user_ids: [] } });
    await flushPromises();
    expect(wrapper.find('[aria-label="Kick guest"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain('Banned');
  });
});

describe('idle playback recovery', () => {
  it('shows account authorization in the header while keeping app-credential search available', async () => {
    authorized = false;
    await renderRoom();
    const status = wrapper.get('[aria-label="Spotify disconnected"]');
    expect(status.attributes('data-tooltip')).toBe('Disconnected');
    expect(status.attributes('href')).toBe('/connection');
    expect(status.attributes('target')).toBe('_blank');
    expect(wrapper.find('#spotify-authorization-required').exists()).toBe(false);
    expect(wrapper.find('.spotify-device-panel').exists()).toBe(false);
    expect(wrapper.get('input[placeholder="Search for songs..."]').attributes('disabled')).toBeUndefined();
  });

  it('refreshes authorization when returning from the connection tab', async () => {
    authorized = false;
    await renderRoom();
    authorized = true;
    window.dispatchEvent(new Event('focus'));
    await flushPromises();
    expect(wrapper.find('[aria-label="Spotify connected"]').exists()).toBe(true);
    expect(wrapper.find('.spotify-device-panel').exists()).toBe(true);
  });

  it('updates the header when Spotify OAuth becomes unavailable', async () => {
    await renderRoom();
    mocks.handlers.get('oauth_required')({});
    await flushPromises();
    expect(wrapper.find('[aria-label="Spotify disconnected"]').exists()).toBe(true);
    expect(wrapper.find('.spotify-device-panel').exists()).toBe(false);
  });

  it('shows the empty queue prompt on two lines only after Resume is clicked, without calling play', async () => {
    authorized = false;
    await renderRoom();
    expect(wrapper.find('.empty-queue-prompt').exists()).toBe(false);
    await wrapper.get('[title="Resume playback"]').trigger('click');
    const lines = wrapper.findAll('.empty-queue-prompt span');
    expect(lines.map((line) => line.text())).toEqual(['Add a song first', 'ლ(´•д• ̀ლ']);
    expect(mocks.apiPost).not.toHaveBeenCalled();
  });

  it('clears the empty queue prompt when a song is added', async () => {
    await renderRoom();
    await wrapper.get('[title="Resume playback"]').trigger('click');
    mocks.handlers.get('queue_updated')({ queue: [{ id: 'item', song: { title: 'New song' } }] });
    await flushPromises();
    expect(wrapper.find('.empty-queue-prompt').exists()).toBe(false);
  });

  it('requires authorization before resuming a nonempty queue', async () => {
    authorized = false;
    pending = [{ id: 'item', song: { title: 'Next song' } }];
    await renderRoom();
    await wrapper.get('[title="Resume playback"]').trigger('click');
    expect(mocks.apiPost).not.toHaveBeenCalled();
    expect(mocks.showToast).toHaveBeenCalledWith('Connect Spotify from the link icon above.');
  });

  it('lets an administrator resume a queued song', async () => {
    mocks.userId.value = 'admin';
    pending = [{ id: 'item', song: { id: 'song', title: 'Next song', artist: 'Artist' } }];
    await renderRoom();
    await wrapper.get('[title="Resume playback"]').trigger('click');
    await flushPromises();
    expect(mocks.apiPost).toHaveBeenCalledWith('/playback/play', { song_id: 'song' });
  });

  it.each([false, true])('hides playback and device controls from ordinary members (guest: %s)', async (isGuest) => {
    mocks.userId.value = 'guest';
    mocks.isGuest.value = isGuest;
    mocks.guestRequestAvailableAt.value = new Date(Date.now() + 300000).toISOString();
    await renderRoom();
    expect(wrapper.find('[title="Resume playback"]').exists()).toBe(false);
    expect(wrapper.find('.playback-controls').exists()).toBe(false);
    expect(wrapper.find('.spotify-device-panel').exists()).toBe(false);
    expect(wrapper.find('[title="Show invite QR"]').exists()).toBe(false);
    expect(wrapper.find('.member-actions').exists()).toBe(false);
    expect(wrapper.find('.queue-pin').exists()).toBe(false);
    expect(wrapper.find('.queue-jump').exists()).toBe(false);
    expect(wrapper.find('.queue-dequeue').exists()).toBe(false);
  });

  it('does not auto-resume from browser song notifications after a pause', async () => {
    await renderRoom();
    await mocks.handlers.get('song_changed')({ song: null });
    expect(mocks.apiPost).not.toHaveBeenCalled();
  });

  it('keeps a pending track visible and Resume available if vote-skip playback has not started', async () => {
    pending = [{ id: 'next', song: { id: 'song', title: 'Waiting song', artist: 'Artist' } }];
    await renderRoom();
    await mocks.handlers.get('skip_vote')({ skip_triggered: true });
    await flushPromises();
    expect(wrapper.find('[title="Resume playback"]').exists()).toBe(true);
    expect(wrapper.get('.queue-panel .track-item-title').text()).toBe('Waiting song');
  });
});

describe('queue priority controls', () => {
  beforeEach(() => {
    pending = ['First', 'Second', 'Third'].map((title, index) => ({
      id: `item-${index}`,
      is_pinned: false,
      song: { id: `song-${index}`, title, artist: 'Artist' },
    }));
  });

  it('hides Jump on the first pending track and updates the displayed order after jumping', async () => {
    await renderRoom();
    expect(wrapper.find('[aria-label="Jump First"]').exists()).toBe(false);
    mocks.apiPost.mockImplementation(async () => { pending = [pending[2], pending[0], pending[1]]; });
    await wrapper.get('[aria-label="Jump Third"]').trigger('click');
    await flushPromises();
    expect(mocks.apiPost).toHaveBeenCalledWith('/queue/item-2/jump');
    expect(wrapper.findAll('.queue-panel .track-item-title').map((item) => item.text())).toEqual(['Third', 'First', 'Second']);
  });

  it('uses matching icons and tooltips for queue actions', async () => {
    await renderRoom();
    expect(wrapper.get('[aria-label="Jump Second"] .ph-arrow-up').exists()).toBe(true);
    expect(wrapper.get('[aria-label="Jump Second"]').attributes('data-tooltip')).toBe('Jump');
    expect(wrapper.get('[aria-label="Pin Second"] .ph-push-pin').exists()).toBe(true);
    expect(wrapper.get('[aria-label="Dequeue Second"] .ph-x').exists()).toBe(true);
    expect(wrapper.get('[aria-label="Dequeue First"]').attributes('data-tooltip')).toBe('Dequeue');
  });

  it('lets an administrator dequeue a song and reloads the queue', async () => {
    mocks.userId.value = 'admin';
    await renderRoom();
    mocks.apiDelete.mockImplementation(async () => { pending = pending.filter((item) => item.id !== 'item-1'); });
    await wrapper.get('[aria-label="Dequeue Second"]').trigger('click');
    await flushPromises();
    expect(mocks.apiDelete).toHaveBeenCalledWith('/queue/item-1');
    expect(wrapper.findAll('.queue-panel .track-item-title').map((item) => item.text())).toEqual(['First', 'Third']);
  });

  it('lets administrators toggle Pin and displays the returned pinned priority', async () => {
    mocks.userId.value = 'admin';
    await renderRoom();
    mocks.apiPatch.mockImplementation(async (_path, body) => {
      pending[1].is_pinned = body.is_pinned;
      pending = [pending[1], pending[0], pending[2]];
    });
    await wrapper.get('[aria-label="Pin Second"]').trigger('click');
    await flushPromises();
    expect(mocks.apiPatch).toHaveBeenCalledWith('/queue/item-1/pin', { is_pinned: true });
    expect(wrapper.get('.queue-pinned .track-item-title').text()).toBe('Second');
    expect(wrapper.get('[aria-label="Unpin Second"]').attributes('aria-pressed')).toBe('true');
  });

  it('prevents a sixth Pin while allowing existing pins to be removed', async () => {
    pending = Array.from({ length: 6 }, (_, index) => ({
      id: `item-${index}`, is_pinned: index < 5,
      song: { title: `Song ${index}`, artist: 'Artist' },
    }));
    await renderRoom();
    expect(wrapper.get('[aria-label="Pin Song 5"]').attributes('disabled')).toBeDefined();
    expect(wrapper.get('[aria-label="Unpin Song 0"]').attributes('disabled')).toBeUndefined();
  });

  it('shows pinned styling but no queue management buttons to ordinary members', async () => {
    mocks.userId.value = 'guest';
    pending[0].is_pinned = true;
    await renderRoom();
    expect(wrapper.find('.queue-pinned').exists()).toBe(true);
    expect(wrapper.find('.queue-pin').exists()).toBe(false);
    expect(wrapper.find('.queue-jump').exists()).toBe(false);
    expect(wrapper.find('.queue-dequeue').exists()).toBe(false);
  });
});


describe('Spotify manager status and devices', () => {
  it('shows devices only when a user is the host of the current room', async () => {
    mocks.userId.value = 'another-room-owner';
    await renderRoom();
    expect(wrapper.find('.spotify-device-panel').exists()).toBe(false);
    expect(mocks.apiGet).not.toHaveBeenCalledWith('/playback/devices');
    wrapper.unmount();
    room.host_user_id = 'another-room-owner';
    await renderRoom();
    expect(wrapper.find('.spotify-device-panel').exists()).toBe(true);
    expect(mocks.apiGet).toHaveBeenCalledWith('/playback/devices');
  });

  it('shows the room host authorization status to administrators', async () => {
    mocks.userId.value = 'admin';
    await renderRoom();
    expect(mocks.apiGet).toHaveBeenCalledWith('/playback/spotify/status');
    expect(mocks.apiGet).not.toHaveBeenCalledWith('/platform-connections/');
    expect(wrapper.find('[aria-label="Spotify connected"]').exists()).toBe(true);
    expect(wrapper.get('a[aria-label="Profile"] .ph-user-circle').exists()).toBe(true);
    expect(wrapper.get('.spotify-status').element.tagName).toBe('SPAN');
    expect(wrapper.find('.spotify-status[href]').exists()).toBe(false);
    expect(wrapper.find('.spotify-device-panel').exists()).toBe(false);
    expect(mocks.apiGet).not.toHaveBeenCalledWith('/playback/devices');
  });

  it('does not request or reveal Spotify status and devices to ordinary members', async () => {
    mocks.userId.value = 'guest';
    await renderRoom();
    window.dispatchEvent(new Event('focus'));
    mocks.handlers.get('oauth_required')({});
    await flushPromises();
    expect(mocks.apiGet).not.toHaveBeenCalledWith('/playback/spotify/status');
    expect(mocks.apiGet).not.toHaveBeenCalledWith('/playback/devices');
    expect(wrapper.find('.spotify-status').exists()).toBe(false);
    expect(wrapper.find('.spotify-device-panel').exists()).toBe(false);
  });

  it('hides Spotify status and devices immediately after administrator access is revoked', async () => {
    mocks.userId.value = 'admin';
    await renderRoom();
    mocks.handlers.get('admins_updated')({ payload: { admin_user_ids: [] } });
    await flushPromises();
    expect(wrapper.find('.spotify-status').exists()).toBe(false);
    expect(wrapper.find('.spotify-device-panel').exists()).toBe(false);
  });

  it('lets the host select an available playback device without requesting play', async () => {
    devices.push({ id: 'speaker', name: 'Speaker', type: 'speaker', is_active: false, is_restricted: false });
    devices.push({ id: 'restricted', name: 'Restricted', type: 'speaker', is_active: false, is_restricted: true });
    await renderRoom();
    expect(wrapper.findAll('#spotify-device option').map((option) => option.text())).toEqual(['Select a device', 'Desktop (active)', 'Speaker']);
    mocks.apiPut.mockImplementation(async (_path, body) => { selectedDeviceId = body.device_id; });
    await wrapper.get('#spotify-device').setValue('speaker');
    await flushPromises();
    expect(mocks.apiPut).toHaveBeenCalledWith('/playback/device', { device_id: 'speaker' });
    expect(mocks.apiPost).not.toHaveBeenCalled();
    expect(wrapper.get('#spotify-device').element.value).toBe('speaker');
  });

  it('distinguishes an offline device from missing account authorization and supports refreshing devices', async () => {
    devices = [];
    selectedDeviceId = null;
    await renderRoom();
    expect(wrapper.find('[aria-label="Spotify connected"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('No Spotify devices are available. Keep Spotify open, then refresh devices.');
    expect(wrapper.get('a[href="https://open.spotify.com/"]').attributes('target')).toBe('_blank');
    devices = [{ id: 'desktop', name: 'Desktop', is_active: true }];
    await wrapper.get('.spotify-device-controls button').trigger('click');
    await flushPromises();
    expect(wrapper.get('#spotify-device').element.value).toBe('desktop');
    expect(wrapper.find('.spotify-device-help').exists()).toBe(false);
  });
});

describe('Spotify shared request cooldown', () => {
  beforeEach(() => { vi.useFakeTimers(); });

  it('hydrates the cooldown without probing devices and re-enables controls after the deadline', async () => {
    mocks.apiPost.mockResolvedValue({});
    rateLimitUntil = new Date(Date.now() + 120000).toISOString();
    pending = [{ id: 'item', song: { id: 'song', title: 'Queued song' } }];
    await renderRoom();
    expect(wrapper.get('.spotify-cooldown').text()).toContain('2:00');
    expect(wrapper.find('[aria-label="Spotify connected"]').exists()).toBe(true);
    expect(wrapper.get('[title="Resume playback"]').attributes('disabled')).toBeDefined();
    expect(wrapper.get('[placeholder="Search for songs..."]').attributes('disabled')).toBeDefined();
    expect(wrapper.get('.spotify-device-controls button').attributes('disabled')).toBeDefined();
    expect(mocks.apiGet).not.toHaveBeenCalledWith('/playback/devices');
    const requestCount = mocks.apiGet.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60000);
    expect(wrapper.get('.spotify-cooldown').text()).toContain('1:00');
    expect(mocks.apiGet).toHaveBeenCalledTimes(requestCount);
    await vi.advanceTimersByTimeAsync(60000);
    expect(wrapper.find('.spotify-cooldown').exists()).toBe(false);
    expect(wrapper.get('[title="Resume playback"]').attributes('disabled')).toBeUndefined();
    expect(mocks.apiPost).not.toHaveBeenCalled();
    await wrapper.get('[title="Resume playback"]').trigger('click');
    await flushPromises();
    expect(mocks.apiPost).toHaveBeenCalledOnce();
  });

  it('turns a rejected Resume into one shared countdown without follow-up device requests', async () => {
    pending = [{ id: 'item', song: { id: 'song', title: 'Queued song' } }];
    await renderRoom();
    const deviceRequests = mocks.apiGet.mock.calls.filter(([path]) => path === '/playback/devices').length;
    mocks.apiPost.mockRejectedValue({ code: 'SPOTIFY_RATE_LIMITED', status: 429, retryAfter: 22699 });
    await wrapper.get('[title="Resume playback"]').trigger('click');
    await flushPromises();
    expect(wrapper.get('.spotify-cooldown').text()).toContain('6:18:19');
    expect(wrapper.find('[aria-label="Spotify connected"]').exists()).toBe(true);
    expect(wrapper.find('[aria-label="Spotify disconnected"]').exists()).toBe(false);
    expect(mocks.apiGet.mock.calls.filter(([path]) => path === '/playback/devices')).toHaveLength(deviceRequests);
    await wrapper.get('[title="Resume playback"]').trigger('click');
    expect(mocks.apiPost).toHaveBeenCalledOnce();
  });

  it.each(['admin', 'guest'])('shares availability with %s without revealing playback devices', async (actor) => {
    mocks.userId.value = actor;
    rateLimitUntil = new Date(Date.now() + 60000).toISOString();
    await renderRoom();
    expect(wrapper.find('.spotify-cooldown').exists()).toBe(true);
    expect(wrapper.find('.spotify-device-panel').exists()).toBe(false);
    expect(mocks.apiGet).not.toHaveBeenCalledWith('/playback/devices');
    if (actor === 'guest') {
      expect(wrapper.find('.spotify-status').exists()).toBe(false);
      expect(mocks.apiGet).not.toHaveBeenCalledWith('/playback/spotify/status');
    }
  });

  it('updates open members from the shared cooldown event without polling Spotify', async () => {
    mocks.userId.value = 'guest';
    await renderRoom();
    const requests = mocks.apiGet.mock.calls.length;
    mocks.handlers.get('spotify_rate_limited')({ retry_at: new Date(Date.now() + 90000).toISOString(), retry_after: 90 });
    await flushPromises();
    expect(wrapper.get('.spotify-cooldown').text()).toContain('1:30');
    expect(wrapper.get('[placeholder="Search for songs..."]').attributes('disabled')).toBeDefined();
    expect(mocks.apiGet).toHaveBeenCalledTimes(requests);
  });

  it('handles search limits separately from a guest request allowance', async () => {
    mocks.isGuest.value = true;
    mocks.userId.value = 'guest';
    mocks.guestRequestAvailableAt.value = new Date(Date.now() - 1000).toISOString();
    const defaultGet = mocks.apiGet.getMockImplementation();
    mocks.apiGet.mockImplementation(async (path) => {
      if (path.startsWith('/search/?')) throw { code: 'SPOTIFY_RATE_LIMITED', status: 429, retryAfter: 120 };
      return defaultGet(path);
    });
    await renderRoom();
    await wrapper.get('[placeholder="Search for songs..."]').setValue('Song');
    await vi.advanceTimersByTimeAsync(300);
    await flushPromises();
    expect(wrapper.get('.spotify-cooldown').text()).toContain('2:00');
    expect(wrapper.get('.guest-request-notice').text()).toContain('You can request a song now');
    expect(mocks.apiPost).not.toHaveBeenCalled();
  });
});
