import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import InvitePage from './InvitePage.vue';

const mocks = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  push: vi.fn(),
  replace: vi.fn(),
}));
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
  useRoute: () => ({ query: { token: 'invite-token', join: '1' } }),
}));
vi.mock('../composables/useApi.js', () => ({ apiGet: mocks.apiGet, apiPost: mocks.apiPost }));
vi.mock('../composables/useAuth.js', () => ({
  useAuth: () => ({ isAuthenticated: { value: true } }),
}));
vi.mock('../components/ThemeToggle.vue', () => ({ default: { template: '<span />' } }));

let wrapper;
beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiGet.mockImplementation(async (path) =>
    path.includes('/admission/status')
      ? { status: 'approved' }
      : { room_id: 'room', room_name: 'Invited room' },
  );
  mocks.apiPost.mockResolvedValue({
    room_id: 'room',
    room_name: 'Invited room',
    status: 'pending',
  });
});
afterEach(() => wrapper?.unmount());

describe('invitation admission', () => {
  it('shows approval instructions after login instead of navigating straight into the room', async () => {
    wrapper = mount(InvitePage);
    await flushPromises();
    expect(mocks.apiPost).toHaveBeenCalledWith('/rooms/invite/invite-token/join', {});
    expect(wrapper.text()).toContain(
      'Please contact the room host or an administrator for approval.',
    );
    expect(mocks.push).not.toHaveBeenCalled();
    mocks.apiPost.mockResolvedValue({ room_id: 'room', status: 'joined' });
    await wrapper
      .findAll('button')
      .find((button) => button.text() === 'Check status')
      .trigger('click');
    await flushPromises();
    expect(mocks.apiPost).toHaveBeenLastCalledWith('/rooms/invite/invite-token/join', {});
    expect(mocks.push).toHaveBeenCalledWith('/room/room');
  });

  it('does not turn a banned invitation into a pending approval request', async () => {
    mocks.apiPost.mockRejectedValue({ status: 403, detail: 'You are banned from this room' });
    wrapper = mount(InvitePage);
    await flushPromises();
    expect(wrapper.text()).toContain('You are banned from this room');
    expect(wrapper.find('.admission-prompt').exists()).toBe(false);
    expect(mocks.push).not.toHaveBeenCalled();
  });
});
