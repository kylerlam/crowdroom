<template>
  <div class="page room-page">
    <header class="room-header">
      <h2 :title="roomName">{{ roomName }}</h2>
      <div class="header-actions">
        <span class="badge">{{ roomCode }}</span>
        <a href="/profile" target="_blank" rel="noopener" class="btn btn-ghost btn-icon icon-tooltip" aria-label="Profile" data-tooltip="Profile">
          <i class="ph ph-user-circle" aria-hidden="true"></i>
        </a>
        <a
          v-if="isHost && spotifyConnectionKnown"
          href="/connection"
          target="_blank"
          rel="noopener"
          class="btn btn-ghost btn-icon icon-tooltip spotify-status"
          :class="{ connected: !spotifyPlaybackRequired }"
          :aria-label="spotifyPlaybackRequired ? 'Spotify disconnected' : 'Spotify connected'"
          :data-tooltip="spotifyPlaybackRequired ? 'Disconnected' : 'Connected'"
        >
          <i :class="spotifyPlaybackRequired ? 'ph ph-link-break' : 'ph ph-link'" aria-hidden="true"></i>
        </a>
        <span
          v-else-if="isManager && spotifyConnectionKnown"
          class="btn btn-ghost btn-icon icon-tooltip spotify-status"
          :class="{ connected: !spotifyPlaybackRequired }"
          :aria-label="spotifyPlaybackRequired ? 'Spotify disconnected' : 'Spotify connected'"
          :data-tooltip="spotifyPlaybackRequired ? 'Disconnected' : 'Connected'"
          role="img"
          tabindex="0"
        ><i :class="spotifyPlaybackRequired ? 'ph ph-link-break' : 'ph ph-link'" aria-hidden="true"></i></span>
        <button
          v-if="isHost"
          class="btn btn-ghost btn-icon"
          title="Show invite QR"
          @click="openQRModal"
        >
          <i class="ph ph-qr-code"></i>
        </button>
        <ThemeToggle />
        <button class="btn btn-ghost btn-danger" @click="leaveRoom">Leave</button>
      </div>
    </header>

    <main class="room-layout">
      <!-- Now Playing -->
      <section class="panel now-playing-panel">
        <div v-if="!currentSong" class="empty-state">
          <i class="ph ph-play"></i>
          <button
            v-if="isManager"
            class="btn btn-primary"
            :disabled="resuming"
            title="Resume playback"
            @click="resumePlayback"
          >Resume</button>
          <p v-if="showEmptyQueuePrompt && queueItems.length === 0" class="empty-queue-prompt" role="status">
            <span>Add a song first</span>
            <span class="empty-queue-face">ლ(´•д• ̀ლ</span>
          </p>
        </div>
        <div v-else>
          <img
            class="album-art"
            :src="currentSong.song.album_art_url || ALBUM_ART_PLACEHOLDER"
            :alt="`${currentSong.song.title} album art`"
            @error="onArtError"
          />
          <div class="track-info">
            <h3>{{ currentSong.song.title }}</h3>
            <p class="text-secondary">{{ currentSong.song.artist }}</p>
          </div>
          <div class="progress-container">
            <div class="progress-bar">
              <div class="progress-fill" :style="{ width: progressPercent + '%' }"></div>
            </div>
            <div class="progress-times">
              <span>{{ formatTime(elapsedMs) }}</span>
              <span>{{ formatTime(currentSong.song.duration * 1000) }}</span>
            </div>
          </div>
          <div v-if="currentSong.id" class="now-playing-actions">
            <button
              class="btn btn-ghost"
              :class="{ active: hasVotedSkip, 'on-cooldown': voteOnCooldown }"
              :title="hasVotedSkip ? 'Undo skip vote' : 'Vote to skip'"
              @click="voteSkip(currentSong.id)"
            >
              <i class="ph ph-skip-forward"></i> VoteSkip
              <span class="skip-vote-badge">{{ currentSong.votes_skip || 0 }}</span>
            </button>
          </div>
          <div v-if="isManager" class="playback-controls">
            <button class="btn btn-icon" title="Play/Pause" :disabled="spotifyPlaybackRequired" @click="togglePlayPause">
              <i :class="isPlaying ? 'ph ph-pause' : 'ph ph-play'"></i>
            </button>
            <button class="btn btn-icon" title="Skip" :disabled="spotifyPlaybackRequired" @click="hostSkip">
              <i class="ph ph-skip-forward"></i>
            </button>
          </div>
        </div>
        <details v-if="isHost && spotifyConnectionKnown && !spotifyPlaybackRequired" class="spotify-device-panel" @toggle="onDevicesToggle">
          <summary><i class="ph ph-speaker-high" aria-hidden="true"></i> Playback device</summary>
          <div class="spotify-device-controls">
            <label for="spotify-device">Spotify device</label>
            <select
              id="spotify-device"
              v-model="selectedDeviceId"
              class="input"
              :disabled="devicesLoading || deviceSelecting || !availableDevices.length"
              @change="selectSpotifyDevice"
            >
              <option value="" disabled>Select a device</option>
              <option v-for="device in availableDevices" :key="device.id" :value="device.id">
                {{ device.name }}{{ device.is_active ? ' (active)' : '' }}
              </option>
            </select>
            <button class="btn btn-secondary" :disabled="devicesLoading || deviceSelecting" @click="loadSpotifyDevices">
              <i class="ph ph-arrow-clockwise" aria-hidden="true"></i> Refresh devices
            </button>
          </div>
          <p v-if="deviceError" class="error-text" role="status">{{ deviceError }}</p>
          <p v-else-if="!devicesLoading && !availableDevices.length" class="text-tertiary spotify-device-help" role="status">
            No Spotify devices are available. Keep Spotify open, then refresh devices.
          </p>
          <a href="https://open.spotify.com/" target="_blank" rel="noopener" class="spotify-device-link">Open Spotify</a>
        </details>
      </section>

      <!-- Queue -->
      <section class="panel queue-panel">
        <details open>
          <summary>
            <h3><i class="ph ph-queue"></i> Queue <span class="badge">{{ queueItems.length }}</span></h3>
          </summary>
          <div class="track-list">
            <TrackItem
              v-for="(item, index) in queueItems"
              :key="item.id"
              :track="item.song"
              :added-by="item.added_by?.username || ''"
              :class="{ 'queue-pinned': item.is_pinned }"
            >
              <template #actions>
                <span v-if="item.is_pinned" class="pin-badge" role="img" aria-label="Pinned" title="Pinned priority">
                  <i class="ph ph-push-pin"></i>
                </span>
                <template v-if="isManager">
                  <button
                    class="btn btn-ghost queue-pin icon-tooltip"
                    :class="{ active: item.is_pinned }"
                    :aria-label="`${item.is_pinned ? 'Unpin' : 'Pin'} ${item.song.title}`"
                    :aria-pressed="!!item.is_pinned"
                    :disabled="queueBusyIds.has(item.id) || (!item.is_pinned && pinnedCount >= 5)"
                    :data-tooltip="item.is_pinned ? 'Unpin' : pinnedCount >= 5 ? 'Maximum 5 pinned songs' : 'Pin'"
                    @click="setQueuePin(item)"
                  ><i :class="item.is_pinned ? 'ph ph-push-pin-slash' : 'ph ph-push-pin'" aria-hidden="true"></i></button>
                  <button
                    v-if="index > 0"
                    class="btn btn-ghost queue-jump icon-tooltip"
                    :aria-label="`Jump ${item.song.title}`"
                    :disabled="queueBusyIds.has(item.id)"
                    data-tooltip="Jump"
                    @click="jumpQueueItem(item)"
                  ><i class="ph ph-arrow-up" aria-hidden="true"></i></button>
                  <button
                    class="btn btn-ghost queue-dequeue icon-tooltip"
                    :aria-label="`Dequeue ${item.song.title}`"
                    :disabled="queueBusyIds.has(item.id)"
                    data-tooltip="Dequeue"
                    @click="dequeueItem(item)"
                  ><i class="ph ph-x" aria-hidden="true"></i></button>
                </template>
              </template>
            </TrackItem>
          </div>
          <div v-if="queueItems.length === 0" class="empty-state">
            <i class="ph ph-playlist"></i>
            <p>Queue is empty. Search and add songs!</p>
          </div>
        </details>
      </section>

      <!-- Search -->
      <section class="panel search-panel">
        <details @toggle="onSearchToggle">
          <summary>
            <h3><i class="ph ph-magnifying-glass"></i> Search</h3>
          </summary>
          <div v-if="spotifyBanner" class="empty-state">
            <i class="ph ph-spotify-logo"></i>
            <p>{{ spotifyBanner.message }}</p>
            <a v-if="isHost" href="/connection" target="_blank" rel="noopener" class="btn btn-primary" style="margin-top: var(--space-3)">Set up Spotify</a>
          </div>
          <div class="search-input-wrapper">
            <i class="ph ph-magnifying-glass"></i>
            <input
              ref="searchInput"
              v-model="searchQuery"
              type="text"
              class="input"
              placeholder="Search for songs..."
              autocomplete="off"
              :disabled="!!spotifyBanner"
              @input="debouncedSearch"
            />
          </div>
          <p v-if="isGuest" class="guest-request-notice text-secondary">
            {{ guestRequestMessage }}
          </p>
          <div class="track-list">
            <TrackItem v-for="track in searchResults" :key="track.external_id" :track="track">
              <template #actions>
                <button
                  class="btn btn-secondary"
                  :title="isGuest && guestRequestBlocked ? guestRequestMessage : 'Add to queue'"
                  :aria-label="`Add ${track.title} to queue`"
                  :disabled="addingIds.has(track.external_id) || (isGuest && (guestRequestBlocked || addingIds.size > 0))"
                  @click="addToQueue(track.external_id)"
                >
                  <i class="ph ph-plus"></i>
                </button>
              </template>
            </TrackItem>
          </div>
          <div
            v-if="searchQuery.length >= 2 && searchResults.length === 0 && !searching"
            class="empty-state"
          >
            <p>No results found.</p>
          </div>
        </details>
      </section>

      <!-- Members -->
      <aside class="panel members-panel">
        <details>
          <summary>
            <h3>
              <i class="ph ph-users"></i> Members <span class="badge">{{ members.length }}</span>
            </h3>
          </summary>
          <ul class="members-list">
            <li v-for="member in members" :key="member.id" class="member-item">
              <img
                v-if="member.avatar_url"
                :src="member.avatar_url"
                class="member-avatar member-avatar-img"
                alt=""
              />
              <span v-else class="member-avatar">{{
                (member.username || '?')[0].toUpperCase()
              }}</span>
              <span class="member-name">{{ member.username || 'Unknown' }}</span>
              <span v-if="member.id === hostUserId" class="member-role">Host</span>
              <span v-else-if="adminUserIds.includes(member.id)" class="member-role">Admin</span>
              <span
                v-if="canManageMember(member)"
                class="member-actions"
              >
                <button
                  v-if="isHost && !member.is_guest"
                  type="button"
                  class="member-action-btn"
                  :aria-label="`${adminUserIds.includes(member.id) ? 'Revoke admin from' : 'Make admin:'} ${member.username || 'user'}`"
                  :title="adminUserIds.includes(member.id) ? 'Revoke admin' : 'Make admin'"
                  @click="toggleAdmin(member)"
                >
                  <i :class="adminUserIds.includes(member.id) ? 'ph ph-shield-slash' : 'ph ph-shield-check'"></i>
                </button>
                <button
                  type="button"
                  class="member-action-btn"
                  :aria-label="`Kick ${member.username || 'user'}`"
                  title="Kick"
                  @click="kickMember(member)"
                >
                  <i class="ph ph-sign-out"></i>
                </button>
                <button
                  type="button"
                  class="member-action-btn member-action-ban"
                  :aria-label="`Ban ${member.username || 'user'}`"
                  title="Ban"
                  @click="banMember(member)"
                >
                  <i class="ph ph-prohibit"></i>
                </button>
              </span>
            </li>
          </ul>
        </details>
      </aside>

      <!-- Join requests -->
      <aside v-if="isManager" class="panel members-panel admissions-panel">
        <details @toggle="onAdmissionsToggle">
          <summary>
            <h3><i class="ph ph-user-plus"></i> Join requests <span class="badge">{{ admissions.filter(request => request.status === 'pending').length }}</span></h3>
          </summary>
          <p v-if="admissionError" class="error-text" role="status">{{ admissionError }}</p>
          <p v-if="!admissions.length" class="text-secondary">No pending requests.</p>
          <ul class="members-list">
            <li v-for="request in admissions" :key="request.user_id" class="member-item">
              <div class="admission-identity">
                <span class="member-name">{{ request.username }}</span>
                <UserIdentity :public-id="request.public_id" />
              </div>
              <span v-if="request.status === 'rejected'" class="badge">Declined</span>
              <span class="member-actions">
                <button class="btn btn-secondary" :aria-label="`Approve ${request.username}`" :disabled="admissionBusyIds.has(request.user_id)" @click="reviewAdmission(request, true)">Approve</button>
                <button v-if="request.status === 'pending'" class="btn btn-ghost" :aria-label="`Deny ${request.username}`" :disabled="admissionBusyIds.has(request.user_id)" @click="reviewAdmission(request, false)">Deny</button>
              </span>
            </li>
          </ul>
          <button class="btn btn-ghost" @click="loadAdmissions">Refresh requests</button>
        </details>
      </aside>

      <!-- Banned users -->
      <aside v-if="isManager" class="panel members-panel">
        <details @toggle="onBansToggle">
          <summary>
            <h3>
              <i class="ph ph-prohibit"></i> Banned
              <span class="badge">{{ bannedUsers.length }}</span>
            </h3>
          </summary>
          <ul class="members-list">
            <li
              v-for="banned in bannedUsers"
              :key="banned.user_id"
              class="member-item"
            >
              <span class="member-avatar">{{
                (banned.username || '?')[0].toUpperCase()
              }}</span>
              <span>{{ banned.username || 'Unknown' }}</span>
              <span class="member-actions">
                <button
                  type="button"
                  class="member-action-btn"
                  :aria-label="`Unban ${banned.username || 'user'}`"
                  title="Unban"
                  @click="unbanUser(banned)"
                >
                  <i class="ph ph-arrow-counter-clockwise"></i>
                </button>
              </span>
            </li>
          </ul>
          <p
            v-if="bannedUsers.length === 0"
            class="text-tertiary"
            style="padding: var(--space-3)"
          >
            No banned users.
          </p>
        </details>
      </aside>

      <!-- History -->
      <section class="panel history-panel">
        <details>
          <summary>
            <h3>
              <i class="ph ph-clock-counter-clockwise"></i> Recently Played
              <span class="badge">{{ history.length }}</span>
            </h3>
          </summary>
          <div class="track-list">
            <div v-for="entry in history" :key="entry.id" class="track-item">
              <img
                v-if="entry.song?.album_art_url"
                class="track-item-art"
                :src="entry.song.album_art_url"
                alt=""
              />
              <div class="track-item-info">
                <div class="track-item-title">{{ entry.song?.title || entry.song_id }}</div>
                <div class="track-item-artist">
                  {{ entry.song?.artist || '' }} &middot;
                  {{ new Date(entry.played_at).toLocaleTimeString() }}
                </div>
              </div>
            </div>
          </div>
          <p v-if="history.length === 0" class="text-tertiary" style="padding: var(--space-3)">
            No history yet.
          </p>
        </details>
      </section>
    </main>

    <div
      v-if="showLeaveModal"
      class="modal-overlay"
      @click.self="cancelLeave"
    >
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="leave-modal-title">
        <h3 id="leave-modal-title">{{ isHost ? 'Close this room?' : 'Leave this room?' }}</h3>
        <p class="text-tertiary">
          {{ isHost
            ? 'You are the host. Leaving will close the room for everyone.'
            : 'You can rejoin later with the room code.' }}
        </p>
        <div class="modal-actions">
          <button type="button" class="btn btn-secondary" @click="cancelLeave">Cancel</button>
          <button type="button" class="btn btn-danger" @click="confirmLeave">
            {{ isHost ? 'Close room' : 'Leave' }}
          </button>
        </div>
      </div>
    </div>

    <div
      v-if="banTarget"
      class="modal-overlay"
      @click.self="cancelBan"
      @keydown.esc="cancelBan"
    >
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="ban-modal-title">
        <h3 id="ban-modal-title">Ban {{ banTarget.username || 'this user' }}?</h3>
        <p class="text-tertiary">
          They will be removed from the room and blocked from rejoining until you
          unban them.
        </p>
        <div class="modal-actions">
          <button type="button" class="btn btn-secondary" @click="cancelBan">Cancel</button>
          <button type="button" class="btn btn-danger" @click="confirmBan">Ban</button>
        </div>
      </div>
    </div>

    <div
      v-if="showQRModal"
      class="modal-overlay"
      @click.self="closeQRModal"
      @keydown.esc="closeQRModal"
    >
      <div
        class="modal qr-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="qr-modal-title"
      >
        <h3 id="qr-modal-title">Invite via QR</h3>
        <p v-if="qrLoading" class="text-tertiary">Generating invite&hellip;</p>
        <template v-else-if="qrInvite">
          <div class="qr-canvas-wrapper">
            <canvas ref="qrCanvas" aria-label="Invite QR code"></canvas>
          </div>
          <p class="text-tertiary qr-link" :title="qrInvite.url">{{ qrInvite.url }}</p>
          <div class="modal-actions">
            <button type="button" class="btn btn-secondary" @click="copyJoinLink">
              <i class="ph ph-copy"></i> Copy link
            </button>
          </div>
          <hr class="qr-sep" />
          <label for="qr-device-url" class="qr-label">
            Send to device on your network
          </label>
          <div class="qr-device-row">
            <input
              id="qr-device-url"
              v-model="deviceUrl"
              type="url"
              class="input"
              placeholder="http://192.168.1.42"
              :disabled="qrSending"
              @keydown.enter.prevent="sendQRToDevice"
            />
            <button
              type="button"
              class="btn btn-primary"
              :disabled="qrSending || !deviceUrl.trim()"
              @click="sendQRToDevice"
            >
              <i class="ph ph-paper-plane-tilt"></i>
              {{ qrSending ? 'Sending&hellip;' : 'Send' }}
            </button>
          </div>
          <p class="text-tertiary qr-help">
            <code>/send-crowdroom-qr</code> is appended automatically when the path is empty.
          </p>
        </template>
        <div class="modal-actions">
          <button type="button" class="btn btn-secondary" @click="closeQRModal">Close</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue';
import { useRouter, onBeforeRouteLeave } from 'vue-router';
import QRCode from 'qrcode';
import { apiGet, apiPost, apiPut, apiPatch, apiDelete } from '../composables/useApi.js';
import { useAuth } from '../composables/useAuth.js';
import { useWebSocket } from '../composables/useWebSocket.js';
import { useToast } from '../composables/useToast.js';
import ThemeToggle from '../components/ThemeToggle.vue';
import TrackItem from '../components/TrackItem.vue';
import UserIdentity from '../components/UserIdentity.vue';
import { ALBUM_ART_PLACEHOLDER, onArtError } from '../composables/useAlbumArt.js';

const props = defineProps({
  id: { type: String, required: true },
});

const router = useRouter();
const { userId, isGuest, guestRequestAvailableAt, serverClockOffset, fetchMe } = useAuth();
const { connectToRoom, disconnect, onEvent, offEvent } = useWebSocket();
const { showToast } = useToast();

const roomId = ref(props.id);
const roomName = ref('Loading...');
const roomCode = ref('');
const isHost = ref(false);
const hostUserId = ref(null);
const adminUserIds = ref([]);
const isManager = computed(() => isHost.value || adminUserIds.value.includes(userId.value));
const sessionId = ref(null);

const currentSong = ref(null);
const queueItems = ref([]);
const queueBusyIds = ref(new Set());
const pinnedCount = computed(() => queueItems.value.filter((item) => item.is_pinned).length);
const quotaNow = ref(Date.now());
const guestRetryUntil = ref(0);
let guestQuotaTimer = null;
const guestPending = computed(() => queueItems.value.some((item) => item.added_by?.id === userId.value));
const guestWaitSeconds = computed(() => {
  const availableAt = Date.parse(guestRequestAvailableAt?.value);
  const serverDeadline = Number.isFinite(availableAt) ? availableAt - (serverClockOffset?.value || 0) : 0;
  return Math.max(0, Math.ceil((Math.max(serverDeadline, guestRetryUntil.value) - quotaNow.value) / 1000));
});
const guestRequestBlocked = computed(() => !guestRequestAvailableAt?.value || guestPending.value || guestWaitSeconds.value > 0);
const guestRequestMessage = computed(() => {
  const rules = 'Guests wait 5 minutes after first joining, then may request one song every 30 minutes. One queued song at a time.';
  if (guestPending.value) return `Your song is already in the queue. ${rules}`;
  if (guestWaitSeconds.value > 0) {
    const minutes = Math.floor(guestWaitSeconds.value / 60);
    const seconds = String(guestWaitSeconds.value % 60).padStart(2, '0');
    return `You can request your next song in ${minutes}:${seconds}. ${rules}`;
  }
  if (!guestRequestAvailableAt?.value) return `Checking your request allowance… ${rules}`;
  return `You can request a song now. ${rules}`;
});
const members = ref([]);
const bannedUsers = ref([]);
const admissions = ref([]);
const admissionError = ref('');
const admissionBusyIds = ref(new Set());
const history = ref([]);
const searchQuery = ref('');
const searchResults = ref([]);
const searching = ref(false);
const searchInput = ref(null);

// Focus the search field when the Search panel is expanded so the user can
// type immediately. The native <details> toggle event fires after the
// open/closed state has flipped; nextTick ensures the input is in the DOM.
function onSearchToggle(event) {
  if (event.target.open) {
    nextTick(() => searchInput.value?.focus());
  }
}

const isPlaying = ref(false);
const resuming = ref(false);
const showEmptyQueuePrompt = ref(false);
watch(() => queueItems.value.length, (length) => {
  if (length) showEmptyQueuePrompt.value = false;
});
const hasVotedSkip = ref(false);
const voteOnCooldown = ref(false);
const playbackPositionMs = ref(0);
const elapsedMs = ref(0);
let progressFrame = null;
let progressStartTime = null;

const spotifyBanner = ref(null);
const spotifyPlaybackRequired = ref(false);
const spotifyConnectionKnown = ref(false);
const spotifyDevices = ref([]);
const availableDevices = computed(() => spotifyDevices.value.filter((device) => device.id && !device.is_restricted));
const selectedDeviceId = ref('');
const devicesLoading = ref(false);
const deviceSelecting = ref(false);
const deviceError = ref('');
const addingIds = ref(new Set());

const showLeaveModal = ref(false);
// Resolver for the pending leave confirmation. onBeforeRouteLeave awaits this
// promise so navigation pauses until the user picks Cancel or Leave.
let leaveResolver = null;

const showQRModal = ref(false);
const banTarget = ref(null);
const qrInvite = ref(null);
const qrLoading = ref(false);
const qrSending = ref(false);
const qrCanvas = ref(null);
const deviceUrl = ref('');

const progressPercent = computed(() => {
  if (!currentSong.value) return 0;
  const durationMs = currentSong.value.song.duration * 1000;
  return Math.min((elapsedMs.value / durationMs) * 100, 100);
});

let searchTimeout = null;
let searchController = null;
const wsHandlers = [];
let leaving = false;

onMounted(async () => {
  window.addEventListener('focus', refreshSpotifyConnection);
  if (!roomId.value) {
    router.push('/rooms');
    return;
  }

  try {
    if (isGuest?.value) {
      await fetchMe();
      quotaNow.value = Date.now();
      guestQuotaTimer = setInterval(() => { quotaNow.value = Date.now(); }, 1000);
    }
    await loadRoom();
    await loadSession();
    await syncPlaybackPosition();
    await Promise.all([loadCurrentSong(), loadMembers(), loadHistory(), loadAdmissions()]);
    await loadQueue();
    if (isPlaying.value && !currentSong.value && currentExternalId) {
      await loadExternalTrack();
    }
    if (isPlaying.value) startProgressAnimation();
    setupWebSocket();
  } catch (err) {
    showToast(err.detail || 'Failed to load room');
  }
});

onUnmounted(() => {
  window.removeEventListener('focus', refreshSpotifyConnection);
  teardownWebSocket();
  clearTimeout(searchTimeout);
  clearInterval(guestQuotaTimer);
  if (searchController) searchController.abort();
  if (progressFrame) cancelAnimationFrame(progressFrame);
});

onBeforeRouteLeave(async () => {
  if (leaving) return true;
  const confirmed = await requestLeaveConfirmation();
  if (!confirmed) return false;
  await sendLeave();
  return true;
});

function requestLeaveConfirmation() {
  // If a confirmation is already pending, reuse it instead of stacking modals.
  if (leaveResolver) return new Promise((resolve) => (leaveResolver = resolve));
  showLeaveModal.value = true;
  return new Promise((resolve) => {
    leaveResolver = resolve;
  });
}

function resolveLeave(confirmed) {
  showLeaveModal.value = false;
  if (leaveResolver) {
    leaveResolver(confirmed);
    leaveResolver = null;
  }
}

function cancelLeave() {
  resolveLeave(false);
}

function confirmLeave() {
  resolveLeave(true);
}

async function openQRModal() {
  showQRModal.value = true;
  qrLoading.value = true;
  qrInvite.value = null;
  try {
    const invite = await apiPost(`/rooms/${roomId.value}/invite-qr`);
    qrInvite.value = invite;
    qrLoading.value = false;
    await nextTick();
    if (qrCanvas.value) {
      await QRCode.toCanvas(qrCanvas.value, invite.url, {
        width: 200,
        margin: 1,
      });
    }
  } catch (err) {
    qrLoading.value = false;
    showToast(err.detail || 'Could not create QR invite');
    showQRModal.value = false;
  }
}

function closeQRModal() {
  showQRModal.value = false;
  qrInvite.value = null;
  deviceUrl.value = '';
}

async function copyJoinLink() {
  if (!qrInvite.value) return;
  try {
    await navigator.clipboard.writeText(qrInvite.value.url);
    showToast('Link copied');
  } catch {
    showToast('Could not copy link');
  }
}

async function sendQRToDevice() {
  const target = deviceUrl.value.trim();
  if (!target) return;
  qrSending.value = true;
  try {
    await apiPost(`/rooms/${roomId.value}/invite-qr/send-to-device`, {
      device_url: target,
    });
    showToast('Sent to device');
  } catch (err) {
    showToast(err.detail || 'Could not reach device');
  } finally {
    qrSending.value = false;
  }
}

async function loadRoom() {
  const room = await apiGet(`/rooms/${roomId.value}`);
  roomName.value = room.room_name;
  roomCode.value = room.room_code;
  isHost.value = room.host_user_id === userId.value;
  hostUserId.value = room.host_user_id;
  adminUserIds.value = room.admin_user_ids || [];
  if (isManager.value) await checkSpotifyConnection();
}

async function loadSession() {
  try {
    const session = await apiGet(`/session/by-room/${roomId.value}`);
    sessionId.value = session.id;
  } catch {
    try {
      const sessions = await apiGet('/session/');
      const match = sessions.find((sess) => sess.room_id === roomId.value);
      if (match) sessionId.value = match.id;
    } catch {
      sessionId.value = null;
    }
  }
}

async function loadQueue() {
  if (!sessionId.value) return;
  const items = await apiGet(`/queue/?session_id=${sessionId.value}`);
  queueItems.value = stripCurrentSongFromQueue(items);
}

async function reorderQueueItem(item, pin) {
  if (!isManager.value || queueBusyIds.value.has(item.id)) return;
  queueBusyIds.value = new Set(queueBusyIds.value).add(item.id);
  try {
    if (pin === undefined) {
      await apiPost(`/queue/${item.id}/jump`);
    } else {
      await apiPatch(`/queue/${item.id}/pin`, { is_pinned: pin });
    }
    await loadQueue();
  } catch (err) {
    showToast(err.detail || 'Could not update queue');
  } finally {
    const next = new Set(queueBusyIds.value);
    next.delete(item.id);
    queueBusyIds.value = next;
  }
}

function jumpQueueItem(item) {
  return reorderQueueItem(item);
}

function setQueuePin(item) {
  return reorderQueueItem(item, !item.is_pinned);
}

async function dequeueItem(item) {
  if (!isManager.value || queueBusyIds.value.has(item.id)) return;
  queueBusyIds.value = new Set(queueBusyIds.value).add(item.id);
  try {
    await apiDelete(`/queue/${item.id}`);
    await loadQueue();
  } catch (err) {
    showToast(err.detail || 'Could not remove song');
  } finally {
    const next = new Set(queueBusyIds.value);
    next.delete(item.id);
    queueBusyIds.value = next;
  }
}

async function loadCurrentSong() {
  if (!sessionId.value) return;
  const item = await apiGet(`/queue/current?session_id=${sessionId.value}`);
  currentSong.value = item?.song ? item : null;
}

function onAdmissionsToggle(event) {
  if (event.target.open) loadAdmissions();
}

async function loadAdmissions() {
  if (!isManager.value) return;
  admissionError.value = '';
  try {
    const requests = await apiGet(`/rooms/${roomId.value}/admissions`);
    if (isManager.value) admissions.value = requests;
  } catch (err) {
    admissionError.value = err.detail || 'Could not load join requests.';
  }
}

async function reviewAdmission(request, approved) {
  if (!isManager.value || admissionBusyIds.value.has(request.user_id)) return;
  admissionBusyIds.value = new Set(admissionBusyIds.value).add(request.user_id);
  try {
    await apiPatch(`/rooms/${roomId.value}/admissions/${request.user_id}`, { approved });
    await loadAdmissions();
    showToast(approved ? 'Join request approved' : 'Join request denied', 'success');
  } catch (err) {
    showToast(err.detail || 'Could not review join request');
    if (err.status === 403) await loadAdmissions();
  } finally {
    const next = new Set(admissionBusyIds.value);
    next.delete(request.user_id);
    admissionBusyIds.value = next;
  }
}

function stripCurrentSongFromQueue(items) {
  if (!items || items.length === 0) return [];
  const first = items[0];
  const cur = currentSong.value;
  if (cur && cur.id && first.id === cur.id) {
    return items.slice(1);
  }
  if (cur && cur.song && first.song && first.song.external_id === cur.song.external_id) {
    return items.slice(1);
  }
  return items;
}

async function loadMembers() {
  members.value = await apiGet(`/rooms/${roomId.value}/members`);
}

async function loadBans() {
  if (!isManager.value) return;
  try {
    bannedUsers.value = await apiGet(`/rooms/${roomId.value}/bans`);
  } catch (err) {
    showToast(err.detail || 'Could not load banned users');
  }
}

function onBansToggle(event) {
  if (event.target.open) loadBans();
}

function canManageMember(member) {
  if (member.id === hostUserId.value) return false;
  return isHost.value || (isManager.value && !adminUserIds.value.includes(member.id));
}

async function toggleAdmin(member) {
  try {
    const room = await apiPatch(`/rooms/${roomId.value}/admins/${member.id}`, {
      is_admin: !adminUserIds.value.includes(member.id),
    });
    adminUserIds.value = room.admin_user_ids || [];
  } catch (err) {
    showToast(err.detail || 'Could not update administrator');
  }
}

async function kickMember(member) {
  try {
    await apiPost(`/rooms/${roomId.value}/kick/${member.id}`);
    showToast(`${member.username || 'User'} kicked`);
    await loadMembers();
  } catch (err) {
    showToast(err.detail || 'Could not kick user');
  }
}

function banMember(member) {
  banTarget.value = member;
}

function cancelBan() {
  banTarget.value = null;
}

async function confirmBan() {
  const member = banTarget.value;
  banTarget.value = null;
  if (!member) return;
  try {
    await apiPost(`/rooms/${roomId.value}/ban/${member.id}`);
    showToast(`${member.username || 'User'} banned`);
    await Promise.all([loadMembers(), loadBans()]);
  } catch (err) {
    showToast(err.detail || 'Could not ban user');
  }
}

async function unbanUser(banned) {
  try {
    await apiDelete(`/rooms/${roomId.value}/ban/${banned.user_id}`);
    showToast(`${banned.username || 'User'} unbanned`);
    await loadBans();
  } catch (err) {
    showToast(err.detail || 'Could not unban user');
  }
}

async function loadHistory() {
  try {
    history.value = await apiGet(`/rooms/${roomId.value}/history/`);
  } catch {
    history.value = [];
  }
}

async function loadPlaybackState() {
  try {
    await syncPlaybackPosition();
    if (isPlaying.value && !currentSong.value) {
      await loadExternalTrack();
    }
    if (isPlaying.value) startProgressAnimation();
  } catch {
    // ignore
  }
}

let currentExternalId = null;

async function syncPlaybackPosition() {
  const state = await apiGet(`/rooms/${roomId.value}/playback`);
  if (state.status === 'playing') {
    isPlaying.value = true;
    playbackPositionMs.value = state.elapsed_ms || 0;
  } else if (state.status === 'paused') {
    isPlaying.value = false;
    playbackPositionMs.value = state.elapsed_ms || 0;
  } else {
    isPlaying.value = false;
    playbackPositionMs.value = 0;
  }
  currentExternalId = state.current_song_id || null;
  elapsedMs.value = playbackPositionMs.value;
}

async function loadExternalTrack() {
  if (!currentExternalId) return;
  try {
    const song = await apiGet(`/songs/by-external/${currentExternalId}?platform=spotify`);
    currentSong.value = { id: null, votes_skip: 0, song };
  } catch {
    // song not yet in DB
  }
}

function startProgressAnimation() {
  if (progressFrame) cancelAnimationFrame(progressFrame);
  if (!currentSong.value || !isPlaying.value) return;

  const offsetMs = playbackPositionMs.value || 0;
  progressStartTime = Date.now();

  function update() {
    const elapsed = offsetMs + (Date.now() - progressStartTime);
    elapsedMs.value = elapsed;
    const durationMs = currentSong.value?.song?.duration * 1000;
    if (durationMs && elapsed < durationMs && isPlaying.value) {
      progressFrame = requestAnimationFrame(update);
    }
  }

  progressFrame = requestAnimationFrame(update);
}

async function checkSpotifyConnection() {
  if (!isManager.value) return;
  try {
    const { authorized } = await apiGet('/playback/spotify/status');
    spotifyPlaybackRequired.value = !authorized;
    spotifyConnectionKnown.value = true;
    if (isHost.value) {
      const { has_credentials } = await apiGet('/platform-connections/spotify/has-app-credentials');
      spotifyBanner.value = has_credentials ? null : {
        message: 'Set up Spotify in Connection to enable search',
      };
    }
    if (authorized && isHost.value) await loadSpotifyDevices();
  } catch {
    // Keep the last known status when Spotify is temporarily unavailable.
  }
}

function refreshSpotifyConnection() {
  if (isGuest?.value) return fetchMe();
  if (isManager.value) return checkSpotifyConnection();
}

function onDevicesToggle(event) {
  if (event.target.open) loadSpotifyDevices();
}

async function loadSpotifyDevices() {
  if (!isHost.value || spotifyPlaybackRequired.value || devicesLoading.value) return;
  devicesLoading.value = true;
  deviceError.value = '';
  try {
    const { devices, selected_device_id } = await apiGet('/playback/devices');
    spotifyDevices.value = devices;
    selectedDeviceId.value = selected_device_id || devices.find((device) => device.is_active && !device.is_restricted)?.id || '';
  } catch (err) {
    if (err.code === 'SPOTIFY_AUTH_REQUIRED') {
      spotifyPlaybackRequired.value = true;
      spotifyConnectionKnown.value = true;
    } else {
      deviceError.value = err.detail || 'Could not load Spotify devices. Try refreshing.';
    }
  } finally {
    devicesLoading.value = false;
  }
}

async function selectSpotifyDevice() {
  if (!isHost.value || !selectedDeviceId.value || deviceSelecting.value) return;
  deviceSelecting.value = true;
  try {
    await apiPut('/playback/device', { device_id: selectedDeviceId.value });
    showToast('Playback device selected', 'success');
  } catch (err) {
    handlePlaybackError(err);
  } finally {
    deviceSelecting.value = false;
    await loadSpotifyDevices();
  }
}

function debouncedSearch() {
  clearTimeout(searchTimeout);
  const query = searchQuery.value.trim();
  if (query.length < 2) {
    searchResults.value = [];
    return;
  }
  searchTimeout = setTimeout(() => handleSearch(query), 300);
}

async function handleSearch(query) {
  // Cancel any in-flight search so out-of-order responses can't overwrite
  // results from a newer query.
  if (searchController) searchController.abort();
  searchController = new AbortController();
  const signal = searchController.signal;

  searching.value = true;
  try {
    searchResults.value = await apiGet(
      `/search/?room_id=${roomId.value}&q=${encodeURIComponent(query)}`,
      { signal },
    );
  } catch (err) {
    if (err.name === 'AbortError') return;
    showToast(err.detail || 'Search failed');
  } finally {
    if (!signal.aborted) searching.value = false;
  }
}

async function addToQueue(externalId) {
  if (!sessionId.value) {
    showToast('No active session');
    return;
  }
  if (addingIds.value.has(externalId)) return;
  if (isGuest?.value && (guestRequestBlocked.value || addingIds.value.size > 0)) return;
  addingIds.value = new Set(addingIds.value).add(externalId);
  try {
    await apiGet(`/search/${externalId}?room_id=${roomId.value}`);
    const dbSong = await apiGet(`/songs/by-external/${externalId}?platform=spotify`);
    await apiPost('/queue/', { session_id: sessionId.value, song_id: dbSong.id, group: 'manual' });
    showToast('Added to queue', 'success');
    await loadQueue();
    if (isGuest?.value) {
      await fetchMe();
      quotaNow.value = Date.now();
    }
  } catch (err) {
    showToast(err.detail || 'Failed to add song');
    if (isGuest?.value && err.status === 429) {
      guestRetryUntil.value = Date.now() + Math.max(0, err.retryAfter || 0) * 1000;
      quotaNow.value = Date.now();
      await fetchMe();
    }
  } finally {
    const next = new Set(addingIds.value);
    next.delete(externalId);
    addingIds.value = next;
  }
}

const VOTE_COOLDOWN_MS = 2500;
let voteCooldownUntil = 0;

async function voteSkip(queueItemId) {
  if (voteOnCooldown.value) {
    const remainingMs = Math.max(0, voteCooldownUntil - Date.now());
    const seconds = Math.ceil(remainingMs / 1000) || 1;
    showToast(`Slow down — you can vote again in ${seconds}s`);
    return;
  }
  voteOnCooldown.value = true;
  voteCooldownUntil = Date.now() + VOTE_COOLDOWN_MS;
  try {
    if (hasVotedSkip.value) {
      await apiDelete(`/queue/vote?queue_item_id=${queueItemId}`);
      hasVotedSkip.value = false;
    } else {
      await apiPost('/queue/vote', { queue_item_id: queueItemId });
      hasVotedSkip.value = true;
    }
  } catch (err) {
    // The server enforces its own cooldown and returns 429 with retry_after.
    if (err.status === 429) {
      const seconds = Math.ceil(err.retryAfter || 0) || 1;
      showToast(`Too many votes — please wait ${seconds}s`);
    } else {
      showToast(err.detail || 'Vote failed');
    }
  }
  setTimeout(() => {
    voteOnCooldown.value = false;
  }, VOTE_COOLDOWN_MS);
}

async function togglePlayPause() {
  try {
    if (isPlaying.value) {
      await apiPost('/playback/pause');
    } else {
      await apiPost('/playback/play', {});
    }
  } catch (err) {
    handlePlaybackError(err);
  }
}

async function resumePlayback() {
  if (!isManager.value || resuming.value) return;
  if (queueItems.value.length === 0) {
    showEmptyQueuePrompt.value = true;
    return;
  }
  showEmptyQueuePrompt.value = false;
  if (spotifyPlaybackRequired.value) {
    showToast(isHost.value ? 'Connect Spotify from the link icon above.' : 'The room host must connect Spotify.');
    return;
  }
  resuming.value = true;
  try {
    await apiPost('/playback/play', { song_id: queueItems.value[0].song.id });
    await loadCurrentSong();
    await loadPlaybackState();
    await loadQueue();
  } catch (err) {
    handlePlaybackError(err);
  } finally {
    resuming.value = false;
  }
}

async function hostSkip() {
  try {
    await apiPost('/playback/skip');
  } catch (err) {
    handlePlaybackError(err);
  }
}

function handlePlaybackError(err) {
  if (err.code === 'SPOTIFY_AUTH_REQUIRED') {
    spotifyPlaybackRequired.value = true;
    spotifyConnectionKnown.value = true;
  } else {
    loadSpotifyDevices();
  }
  const msg = err.detail || 'Playback control failed';
  showToast(msg);
}

function setupWebSocket() {
  connectToRoom(roomId.value);

  const register = (eventType, handler) => {
    onEvent(eventType, handler);
    wsHandlers.push([eventType, handler]);
  };

  register('queue_updated', (msg) => {
    if (msg.queue) {
      queueItems.value = stripCurrentSongFromQueue(msg.queue);
    } else {
      loadQueue();
    }
  });

  register('song_changed', async () => {
    hasVotedSkip.value = false;
    await loadCurrentSong();
    await syncPlaybackPosition();
    if (!currentSong.value && currentExternalId && isPlaying.value) {
      await loadExternalTrack();
    }
    await Promise.all([loadQueue(), loadHistory()]);
    if (isPlaying.value) startProgressAnimation();
  });

  register('skip_vote', async (msg) => {
    if (currentSong.value && currentSong.value.id === msg.queue_item_id) {
      currentSong.value = { ...currentSong.value, votes_skip: msg.current_votes };
    }
    if (msg.skip_triggered) {
      hasVotedSkip.value = false;
      isPlaying.value = false;
      if (progressFrame) cancelAnimationFrame(progressFrame);
      await loadCurrentSong();
      await syncPlaybackPosition();
      await loadQueue();
      loadHistory();
    }
  });

  register('playback_state_changed', async (msg) => {
    if (msg.status === 'playing') {
      isPlaying.value = true;
      await loadCurrentSong();
      await syncPlaybackPosition();
      if (!currentSong.value && currentExternalId) {
        await loadExternalTrack();
      }
      await loadQueue();
      startProgressAnimation();
    } else if (msg.status === 'stopped') {
      isPlaying.value = false;
      currentSong.value = null;
      elapsedMs.value = 0;
      playbackPositionMs.value = 0;
      await loadQueue();
      if (progressFrame) cancelAnimationFrame(progressFrame);
    } else {
      isPlaying.value = false;
      if (progressFrame) cancelAnimationFrame(progressFrame);
    }
  });

  const refreshMembers = async () => {
    if (leaving) return;
    try {
      await loadMembers();
    } catch {
      // The room may have just been closed; the room_closed handler redirects.
    }
  };
  register('member_joined', refreshMembers);
  register('member_left', refreshMembers);
  register('admins_updated', (msg) => {
    const wasManager = isManager.value;
    adminUserIds.value = msg.payload?.admin_user_ids || [];
    if (!wasManager && isManager.value) {
      checkSpotifyConnection();
      loadAdmissions();
    }
    if (!isManager.value) {
      spotifyConnectionKnown.value = false;
      spotifyDevices.value = [];
      admissions.value = [];
    }
  });
  register('admission_requested', loadAdmissions);
  register('admission_updated', loadAdmissions);
  register('oauth_required', () => {
    if (isManager.value) {
      spotifyPlaybackRequired.value = true;
      spotifyConnectionKnown.value = true;
    }
    isPlaying.value = false;
    if (progressFrame) cancelAnimationFrame(progressFrame);
  });

  const handleRemoval = (label) => (msg) => {
    if (msg.payload?.user_id !== userId.value) {
      refreshMembers();
      return;
    }
    if (leaving) return;
    leaving = true;
    showToast(label);
    teardownWebSocket();
    router.push('/rooms');
  };
  register('member_kicked', handleRemoval('You were kicked from this room'));
  register('member_banned', handleRemoval('You were banned from this room'));

  register('room_closed', () => {
    if (leaving) return;
    leaving = true;
    showToast('This room was closed by the host');
    teardownWebSocket();
    router.push('/rooms');
  });
}

function teardownWebSocket() {
  for (const [eventType, handler] of wsHandlers) {
    offEvent(eventType, handler);
  }
  wsHandlers.length = 0;
  disconnect();
}

async function sendLeave() {
  leaving = true;
  try {
    await apiPost(`/rooms/${roomId.value}/leave`);
  } catch {
    // ignore
  }
  teardownWebSocket();
}

async function leaveRoom() {
  // Navigating triggers onBeforeRouteLeave, which shows the confirmation modal
  // and performs sendLeave() if the user confirms.
  router.push('/rooms');
}

function formatTime(ms) {
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, '0')}`;
}
</script>
