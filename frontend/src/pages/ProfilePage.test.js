import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ProfilePage from './ProfilePage.vue';

const mocks = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn(), apiDelete: vi.fn() }));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('../components/ThemeToggle.vue', () => ({ default: { template: '<span />' } }));
vi.mock('../composables/useAuth.js', () => ({
  useAuth: () => ({ username: 'kyler', avatarUrl: null, setAvatarUrl: vi.fn() }),
}));
vi.mock('../composables/useToast.js', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('../composables/useApi.js', () => ({
  apiGet: mocks.apiGet, apiPost: mocks.apiPost, apiDelete: mocks.apiDelete,
}));

let wrapper;
let connections;
beforeEach(() => {
  vi.clearAllMocks();
  connections = [{ platform: 'spotify', connection_type: 'client_credentials', token_expires_at: null }];
  mocks.apiGet.mockImplementation(async (path) => path === '/platform-connections/'
    ? connections : { has_credentials: true });
});
afterEach(() => wrapper?.unmount());

async function renderProfile() {
  wrapper = mount(ProfilePage);
  await flushPromises();
}

describe('Spotify account connection status', () => {
  it('does not label app credentials as a connected playback account', async () => {
    await renderProfile();
    expect(wrapper.find('.connection-status.connected').exists()).toBe(false);
    expect(wrapper.text()).toContain('Not connected');
    expect(wrapper.text()).toContain('Authorize your Spotify account');
    expect(wrapper.findAll('button').some((button) => button.text() === 'Connect Spotify')).toBe(true);
  });

  it('recognizes OAuth with an expired access token as refreshable', async () => {
    connections = [{ platform: 'spotify', connection_type: 'authorization_code', token_expires_at: '2020-01-01T00:00:00' }];
    await renderProfile();
    expect(wrapper.get('.connection-status.connected').text()).toBe('Connected');
  });

  it('requires authorization when an OAuth row has no issued token', async () => {
    connections = [{ platform: 'spotify', connection_type: 'authorization_code', token_expires_at: null }];
    await renderProfile();
    expect(wrapper.find('.connection-status.connected').exists()).toBe(false);
  });

  it('keeps configured app credentials usable after disconnecting an account', async () => {
    connections = [{ platform: 'spotify', connection_type: 'authorization_code', token_expires_at: '2099-01-01T00:00:00' }];
    await renderProfile();
    mocks.apiDelete.mockImplementation(async () => { connections = []; });
    await wrapper.findAll('button').find((button) => button.text() === 'Disconnect').trigger('click');
    await flushPromises();
    expect(wrapper.findAll('button').some((button) => button.text() === 'Connect Spotify')).toBe(true);
  });
});
