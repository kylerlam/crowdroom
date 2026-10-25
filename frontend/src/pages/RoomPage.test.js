import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import RoomPage from './RoomPage.vue';

const mocks = vi.hoisted(() => ({
  userId: { value: 'owner' },
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  handlers: new Map(),
  showToast: vi.fn(),
}));

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
  onBeforeRouteLeave: vi.fn(),
}));
vi.mock('../components/ThemeToggle.vue', () => ({ default: { template: '<span />' } }));
vi.mock('../composables/useAuth.js', () => ({ useAuth: () => ({ userId: mocks.userId }) }));
vi.mock('../composables/useApi.js', () => ({
  apiGet: mocks.apiGet,
  apiPost: mocks.apiPost,
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

beforeEach(() => {
  vi.clearAllMocks();
  mocks.handlers.clear();
  mocks.userId.value = 'owner';
  room = { host_user_id: 'owner', admin_user_ids: ['admin'], room_name: 'Music room', room_code: 'ABC' };
  pending = [];
  mocks.apiGet.mockImplementation(async (path) => {
    if (path === '/rooms/room') return room;
    if (path === '/session/by-room/room') return { id: 'session' };
    if (path === '/rooms/room/playback') return { status: 'stopped' };
    if (path === '/queue/current?session_id=session') return null;
    if (path === '/queue/?session_id=session') return pending;
    if (path === '/rooms/room/members') {
      return ['owner', 'admin', 'guest'].map((id) => ({ id, username: id }));
    }
    return [];
  });
});

afterEach(() => wrapper?.unmount());

async function renderRoom() {
  wrapper = mount(RoomPage, {
    props: { id: 'room' },
    global: { stubs: { ThemeToggle: true, RouterLink: true } },
  });
  await flushPromises();
  return wrapper;
}

describe('room administrator controls', () => {
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
  it('shows the exact empty queue prompt without calling play', async () => {
    await renderRoom();
    expect(wrapper.text()).toContain('Add a song first ლ(´•д• ̀ლ');
    await wrapper.get('[title="Resume playback"]').trigger('click');
    expect(mocks.apiPost).not.toHaveBeenCalled();
  });

  it('lets an administrator resume a queued song', async () => {
    mocks.userId.value = 'admin';
    pending = [{ id: 'item', song: { id: 'song', title: 'Next song', artist: 'Artist' } }];
    await renderRoom();
    await wrapper.get('[title="Resume playback"]').trigger('click');
    await flushPromises();
    expect(mocks.apiPost).toHaveBeenCalledWith('/playback/play', {});
  });

  it('disables resume for ordinary members', async () => {
    mocks.userId.value = 'guest';
    await renderRoom();
    expect(wrapper.get('[title="Only room managers can resume playback"]').attributes('disabled')).toBeDefined();
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
  });
});
