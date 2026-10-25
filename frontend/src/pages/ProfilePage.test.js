import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ProfilePage from './ProfilePage.vue';

const mocks = vi.hoisted(() => ({ apiPost: vi.fn(), apiGet: vi.fn() }));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('../components/ThemeToggle.vue', () => ({ default: { template: '<span />' } }));
vi.mock('../composables/useAuth.js', () => ({
  useAuth: () => ({ username: 'kyler', avatarUrl: null, setAvatarUrl: vi.fn() }),
}));
vi.mock('../composables/useToast.js', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('../composables/useApi.js', () => ({ apiPost: mocks.apiPost, apiGet: mocks.apiGet }));

let wrapper;
beforeEach(() => vi.clearAllMocks());
afterEach(() => wrapper?.unmount());

describe('personal profile', () => {
  it('shows personal settings without loading or rendering Spotify connections', async () => {
    wrapper = mount(ProfilePage);
    await flushPromises();
    expect(wrapper.text()).toContain('Password');
    expect(wrapper.find('[aria-label="Change avatar"]').exists()).toBe(true);
    expect(wrapper.text()).not.toContain('Spotify');
    expect(mocks.apiGet).not.toHaveBeenCalled();
  });

  it('keeps password updates on the profile page', async () => {
    wrapper = mount(ProfilePage);
    await wrapper.get('#current-password').setValue('current-password');
    await wrapper.get('#new-password').setValue('replacement-password');
    await wrapper.get('#confirm-password').setValue('replacement-password');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(mocks.apiPost).toHaveBeenCalledWith('/auth/change-password', {
      current_password: 'current-password', new_password: 'replacement-password',
    });
    expect(wrapper.text()).toContain('Password changed');
  });
});
