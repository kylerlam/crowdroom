<template>
  <div class="page">
    <header class="app-header">
      <button class="btn btn-ghost" @click="router.push('/rooms')">
        <i class="ph ph-arrow-left"></i> Back
      </button>
      <h2>Connection</h2>
      <div class="header-actions"><ThemeToggle /></div>
    </header>
    <main class="container connection-container">
      <section v-if="loading" class="panel" role="status">Checking connection access…</section>
      <section v-else-if="accessError" class="panel" role="status">
        <p class="error-text">{{ accessError }}</p>
        <button class="btn btn-secondary" @click="loadAccess">Try again</button>
      </section>
      <section v-else-if="!canManage" class="panel" role="status">
        <h3>Only the room host can manage Spotify.</h3>
        <p class="text-secondary">
          The room uses the host's Spotify account. Administrators can control playback from the
          room.
        </p>
      </section>
      <template v-else>
        <!-- Streaming Connections -->
        <section class="panel">
          <h3><i class="ph ph-link"></i> Streaming Connections</h3>
          <div class="connections-list">
            <div class="connection-item">
              <div class="connection-info">
                <i class="ph ph-spotify-logo connection-icon"></i>
                <div>
                  <strong>Spotify</strong>
                  <p v-if="spotifyConnected" class="text-secondary connection-status connected">
                    Connected
                  </p>
                  <p v-else class="text-secondary connection-status">Not connected</p>
                  <p v-if="!spotifyConnected && hasAppCredentials" class="text-secondary">
                    Authorize your Spotify account to enable playback.
                  </p>
                </div>
              </div>
              <div class="connection-actions">
                <template v-if="spotifyConnected">
                  <button
                    class="btn btn-secondary"
                    :disabled="spotifyLoading"
                    @click="connectSpotify"
                  >
                    {{ spotifyLoading ? 'Connecting…' : 'Reconnect' }}
                  </button>
                  <button class="btn btn-ghost btn-danger" @click="disconnectSpotify">
                    Disconnect
                  </button>
                </template>
                <template v-else-if="hasAppCredentials">
                  <button
                    class="btn btn-primary"
                    :disabled="spotifyLoading"
                    @click="connectSpotify"
                  >
                    {{ spotifyLoading ? 'Connecting…' : 'Connect Spotify' }}
                  </button>
                  <button class="btn btn-ghost" @click="showSpotifySetup = true">Change App</button>
                </template>
                <template v-else>
                  <button class="btn btn-primary" @click="showSpotifySetup = true">Set up</button>
                </template>
              </div>
            </div>
          </div>
        </section>

        <!-- Spotify App Credentials Modal -->
        <div v-if="showSpotifySetup" class="modal-overlay" @click.self="showSpotifySetup = false">
          <div class="modal">
            <h3>Set up Spotify App</h3>
            <p class="text-secondary" style="margin-bottom: var(--space-4)">
              Create a Spotify Developer app at
              <a href="https://developer.spotify.com/dashboard" target="_blank"
                >developer.spotify.com</a
              >. Set the redirect URI to:
            </p>
            <code class="redirect-uri-display">{{ redirectUri }}</code>
            <form style="margin-top: var(--space-4)" @submit.prevent="saveSpotifyCredentials">
              <div class="input-group">
                <label for="spotify-client-id">Client ID</label>
                <input
                  id="spotify-client-id"
                  v-model="spotifyClientId"
                  type="text"
                  class="input"
                  placeholder="Your Spotify Client ID"
                  required
                />
              </div>
              <div class="input-group" style="margin-top: var(--space-3)">
                <label for="spotify-client-secret">Client Secret</label>
                <input
                  id="spotify-client-secret"
                  v-model="spotifyClientSecret"
                  type="password"
                  class="input"
                  placeholder="Your Spotify Client Secret"
                  required
                />
              </div>
              <div class="modal-actions" style="margin-top: var(--space-4)">
                <button type="button" class="btn btn-secondary" @click="showSpotifySetup = false">
                  Cancel
                </button>
                <button type="submit" class="btn btn-primary" :disabled="spotifyLoading">
                  {{ spotifyLoading ? 'Connecting…' : 'Save & Connect' }}
                </button>
              </div>
            </form>
          </div>
        </div>
      </template>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useRouter } from 'vue-router';
import { apiGet, apiPost, apiDelete } from '../composables/useApi.js';
import { useToast } from '../composables/useToast.js';
import ThemeToggle from '../components/ThemeToggle.vue';

const router = useRouter();
const { showToast } = useToast();
const loading = ref(true);
const canManage = ref(false);
const accessError = ref('');
const spotifyConnected = ref(false);
const hasAppCredentials = ref(false);
const showSpotifySetup = ref(false);
const spotifyClientId = ref('');
const spotifyClientSecret = ref('');
const spotifyLoading = ref(false);
const redirectUri = computed(() => window.location.origin + '/api/v1/auth/spotify/callback');

onMounted(() => {
  window.addEventListener('focus', loadAccess);
  loadAccess();
});
onUnmounted(() => window.removeEventListener('focus', loadAccess));

async function loadAccess() {
  loading.value = true;
  accessError.value = '';
  try {
    const { can_manage } = await apiGet('/platform-connections/spotify/access');
    canManage.value = can_manage;
    if (can_manage) await loadConnections();
    else {
      showSpotifySetup.value = false;
      spotifyClientId.value = '';
      spotifyClientSecret.value = '';
    }
  } catch (err) {
    canManage.value = false;
    accessError.value = err.detail || 'Could not check connection access. Try again.';
  } finally {
    loading.value = false;
  }
}

function handleError(err, fallback) {
  if (err.status === 403) {
    canManage.value = false;
    showSpotifySetup.value = false;
    spotifyClientId.value = '';
    spotifyClientSecret.value = '';
  }
  showToast(err.detail || fallback);
}

async function connectSpotify() {
  if (!canManage.value || spotifyLoading.value) return;
  spotifyLoading.value = true;
  try {
    const { authorize_url } = await apiPost('/auth/spotify/start');
    if (authorize_url) {
      sessionStorage.setItem('spotify_return_page', '/connection');
      window.location.href = authorize_url;
    } else {
      spotifyLoading.value = false;
    }
  } catch (err) {
    spotifyLoading.value = false;
    handleError(err, 'Could not start Spotify connection');
  }
}

async function loadConnections() {
  try {
    const connections = await apiGet('/platform-connections/');
    spotifyConnected.value = connections.some(
      (conn) =>
        conn.platform === 'spotify' &&
        conn.connection_type === 'authorization_code' &&
        !!conn.token_expires_at,
    );
    if (!spotifyConnected.value) {
      const { has_credentials } = await apiGet('/platform-connections/spotify/has-app-credentials');
      hasAppCredentials.value = has_credentials;
    }
  } catch (err) {
    handleError(err, 'Could not load Spotify connection');
  }
}

async function saveSpotifyCredentials() {
  if (!canManage.value || spotifyLoading.value) return;
  if (!spotifyClientId.value.trim() || !spotifyClientSecret.value.trim()) {
    showToast('Both fields are required');
    return;
  }
  spotifyLoading.value = true;
  try {
    await apiPost('/platform-connections/', {
      platform: 'spotify',
      credentials: {
        client_id: spotifyClientId.value.trim(),
        client_secret: spotifyClientSecret.value.trim(),
      },
    });
    showSpotifySetup.value = false;
    spotifyLoading.value = false;
    await connectSpotify();
  } catch (err) {
    spotifyLoading.value = false;
    handleError(err, 'Invalid credentials');
  }
}

async function disconnectSpotify() {
  if (!canManage.value) return;
  try {
    await apiDelete('/platform-connections/spotify');
    await loadConnections();
    showToast('Spotify disconnected');
  } catch (err) {
    handleError(err, 'Failed to disconnect');
  }
}
</script>

<style scoped>
.connection-container {
  max-width: 600px;
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
  padding: var(--space-5) var(--space-4);
}
.connections-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.connection-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-3);
  border-radius: var(--radius-2);
  background: var(--surface-2);
}

.connection-info {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.connection-icon {
  font-size: 2rem;
}

.connection-status {
  margin: 0;
  font-size: 0.85rem;
}

.connection-status.connected {
  color: var(--success);
}

.connection-actions {
  display: flex;
  gap: var(--space-2);
}

.redirect-uri-display {
  display: block;
  padding: var(--space-2) var(--space-3);
  background: var(--surface-2);
  border-radius: var(--radius-1);
  word-break: break-all;
  font-size: 0.85rem;
}

@media (max-width: 480px) {
  .connection-item {
    align-items: flex-start;
    flex-direction: column;
    gap: var(--space-3);
  }
  .connection-actions {
    flex-wrap: wrap;
  }
}
</style>
