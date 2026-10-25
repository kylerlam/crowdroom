import { mount, flushPromises } from '@vue/test-utils';
import { describe, expect, it, vi } from 'vitest';
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
  it('sends an authenticated invite through the approval-aware invitation page', async () => {
    mocks.apiGet.mockResolvedValue({ mode: 'ONLINE' });
    const wrapper = mount(LoginPage);
    await flushPromises();
    await wrapper.get('input[placeholder="Username or email"]').setValue('new-member');
    await wrapper.get('input[placeholder="Password"]').setValue('test-password');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(mocks.apiPost).toHaveBeenCalledExactlyOnceWith('/auth/login', {
      identifier: 'new-member', password: 'test-password',
    });
    expect(mocks.fetchMe).toHaveBeenCalledOnce();
    expect(mocks.push).toHaveBeenCalledWith({ path: '/invite', query: { token: 'invite-token', join: '1' } });
    wrapper.unmount();
  });
});
