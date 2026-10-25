import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import RoomsPage from './RoomsPage.vue';

const mocks = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  push: vi.fn(),
  showToast: vi.fn(),
  isGuest: null,
}));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }));
vi.mock('../composables/useApi.js', () => ({ apiGet: mocks.apiGet, apiPost: mocks.apiPost }));
vi.mock('../composables/useAuth.js', async () => {
  const { ref } = await import('vue');
  mocks.isGuest = ref(false);
  return {
  useAuth: () => ({
    userId: { value: 'new-user' },
    username: { value: 'New user' },
    hasPassword: { value: true },
    fetchMe: async () => ({ id: 'new-user', room_id: null }),
    logout: vi.fn(),
    setHasPassword: vi.fn(),
    isGuest: mocks.isGuest,
  }),
};
});
vi.mock('../composables/useToast.js', () => ({ useToast: () => ({ showToast: mocks.showToast }) }));
vi.mock('../components/ThemeToggle.vue', () => ({ default: { template: '<span />' } }));
vi.mock('../components/PaginationControls.vue', () => ({ default: { template: '<span />' } }));

let wrapper;
let room;
beforeEach(() => {
  vi.clearAllMocks();
  mocks.isGuest.value = false;
  room = { id: 'room', room_name: 'Music room', host_user_id: 'host', is_private: false };
  mocks.apiGet.mockImplementation(async (path) => {
    if (path === '/rooms/') return [room];
    if (path === '/platform-connections/spotify/access') return { can_manage: true };
    if (path === '/rooms/room/admission/status') return { status: 'approved' };
    return [];
  });
  mocks.apiPost.mockResolvedValue({ room_id: 'room', room_name: 'Music room', status: 'pending', public_id: 'CR-4827-9163' });
});
afterEach(() => wrapper?.unmount());

async function renderRooms() {
  wrapper = mount(RoomsPage, { global: { stubs: { RouterLink: true } } });
  await flushPromises();
}

describe('room admission entry points', () => {
  it('lets a guest join immediately without room creation or Spotify connection controls', async () => {
    mocks.isGuest.value = true;
    mocks.apiPost.mockResolvedValue({ room_id: 'room', status: 'joined' });
    await renderRooms();
    expect(wrapper.text()).not.toContain('Create Room');
    expect(mocks.apiGet).not.toHaveBeenCalledWith('/platform-connections/spotify/access');
    await wrapper.get('.room-card .btn-primary').trigger('click');
    await flushPromises();
    expect(wrapper.find('.admission-prompt').exists()).toBe(false);
    expect(mocks.push).toHaveBeenCalledWith('/room/room');
  });
  it('keeps new users outside and displays instructions after a validated request', async () => {
    await renderRooms();
    await wrapper.get('.room-card .btn-primary').trigger('click');
    await flushPromises();
    expect(wrapper.text()).toContain(
      'Your request to join has been sent. Please contact the room host or an administrator for approval.',
    );
    expect(mocks.push).not.toHaveBeenCalled();
    expect(wrapper.get('.admission-prompt .user-identity-value').text()).toBe('CR-4827-9163');
  });

  it('retains a validated private-room PIN after closing the modal and waiting for approval', async () => {
    room.is_private = true;
    await renderRooms();
    await wrapper.get('.room-card .btn-primary').trigger('click');
    await wrapper.get('input[placeholder="Room PIN"]').setValue('1234');
    await wrapper.get('.modal form').trigger('submit');
    await flushPromises();
    expect(wrapper.find('.modal').exists()).toBe(false);
    expect(mocks.push).not.toHaveBeenCalled();
    mocks.apiPost.mockResolvedValue({ room_id: 'room', status: 'joined' });
    await wrapper
      .findAll('button')
      .find((button) => button.text() === 'Check status')
      .trigger('click');
    await flushPromises();
    expect(mocks.apiPost).toHaveBeenLastCalledWith('/rooms/room/join', { pin: '1234' });
    expect(mocks.push).toHaveBeenCalledWith('/room/room');
  });

  it('reports a ban as a ban without misleading new-user or invalid-PIN prompts', async () => {
    room.is_private = true;
    mocks.apiPost.mockRejectedValue({ status: 403, detail: 'You are banned from this room' });
    await renderRooms();
    await wrapper.get('.room-card .btn-primary').trigger('click');
    await wrapper.get('input[placeholder="Room PIN"]').setValue('1234');
    await wrapper.get('.modal form').trigger('submit');
    await flushPromises();
    expect(mocks.showToast).toHaveBeenCalledWith('You are banned from this room');
    expect(wrapper.find('.admission-prompt').exists()).toBe(false);
    expect(mocks.push).not.toHaveBeenCalled();
  });
});
