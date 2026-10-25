import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import LoginPage from './LoginPage.vue';

const mocks = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn(), push: vi.fn(), fetchMe: vi.fn() }));
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mocks.push }),
  useRoute: () => ({ query: { invite: 'invite-token' } }),
}));
vi.mock('../composables/useApi.js', () => ({ apiGet: mocks.apiGet, apiPost: mocks.apiPost }));
vi.mock('../composables/useAuth.js', () => ({ useAuth: () => ({ fetchMe: mocks.fetchMe }) }));
vi.mock('../components/ThemeToggle.vue', () => ({ default: { template: '<span />' } }));

describe('invited login', () => {
  beforeEach(() => vi.clearAllMocks());
  it('sends an authenticated invite through the approval-aware invitation page', async () => {
    mocks.apiGet.mockResolvedValue({ mode: 'ONLINE' });
    const wrapper = mount(LoginPage);
    await flushPromises();
    await wrapper.get('input[placeholder="Username or email"]').setValue('new-member');
    await wrapper.get('input[placeholder="Password"]').setValue('test-password');
    await wrapper.get('.tab-switcher + form').trigger('submit');
    await flushPromises();
    expect(mocks.apiPost).toHaveBeenCalledExactlyOnceWith('/auth/login', {
      identifier: 'new-member', password: 'test-password',
    });
    expect(mocks.fetchMe).toHaveBeenCalledOnce();
    expect(mocks.push).toHaveBeenCalledWith({ path: '/invite', query: { token: 'invite-token', join: '1' } });
    wrapper.unmount();
  });

  it('joins with just a nickname and retains the invite destination', async () => {
    mocks.apiGet.mockResolvedValue({ mode: 'ONLINE' });
    const wrapper = mount(LoginPage);
    await flushPromises();
    await wrapper.get('#guest-name').setValue('  Guest listener  ');
    await wrapper.get('.guest-entry form').trigger('submit');
    await flushPromises();
    expect(mocks.apiPost).toHaveBeenCalledExactlyOnceWith('/auth/guest', { username: 'Guest listener' });
    expect(mocks.fetchMe).toHaveBeenCalledOnce();
    expect(mocks.push).toHaveBeenCalledWith({ path: '/invite', query: { token: 'invite-token', join: '1' } });
    expect(wrapper.text()).toContain('No approval needed to join');
    expect(wrapper.text()).toContain('5 minutes');
    expect(wrapper.text()).toContain('30 minutes');
    wrapper.unmount();
  });

  it('uses the secure guest endpoint for a LOCAL nickname without a password', async () => {
    mocks.apiGet.mockResolvedValue({ mode: 'LOCAL' });
    const wrapper = mount(LoginPage);
    await flushPromises();
    await wrapper.get('#username').setValue('Listener');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(mocks.apiPost).toHaveBeenCalledExactlyOnceWith('/auth/guest', { username: 'Listener' });
    expect(wrapper.find('.guest-entry').exists()).toBe(false);
    wrapper.unmount();
  });
});
