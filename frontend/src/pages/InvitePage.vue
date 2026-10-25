<template>
  <main class="page login-page">
    <div class="login-container">
      <header class="login-header">
        <h1>CrowdRoom</h1>
        <p class="text-secondary">You've been invited!</p>
      </header>

      <div v-if="loading" class="empty-state">
        <p>Loading invite...</p>
      </div>

      <div v-else-if="error" class="error-text">{{ error }}</div>

      <AdmissionPrompt v-else-if="admission" :request="admission" :busy="admissionBusy" :error="admissionError" @check="checkAdmission" @cancel="cancelAdmission" />

      <div v-else-if="preview" class="card">
        <h3>{{ preview.room_name }}</h3>
        <p class="text-secondary">{{ preview.is_private ? 'Private room' : 'Public room' }}</p>
        <button
          class="btn btn-primary btn-full"
          :disabled="admissionBusy"
          style="margin-top: var(--space-4)"
          @click="joinInvite"
        >
          Join Room
        </button>
      </div>
    </div>

    <ThemeToggle class="theme-toggle" />
  </main>
</template>

<script setup>
import { ref, onMounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { apiGet } from '../composables/useApi.js';
import { useAuth } from '../composables/useAuth.js';
import { useRoomAdmission } from '../composables/useRoomAdmission.js';
import ThemeToggle from '../components/ThemeToggle.vue';
import AdmissionPrompt from '../components/AdmissionPrompt.vue';

const route = useRoute();
const router = useRouter();
const { isAuthenticated } = useAuth();
const { admission, busy: admissionBusy, error: admissionError, join: requestJoin, checkStatus: checkAdmission, cancel: cancelAdmission } = useRoomAdmission();

const loading = ref(true);
const error = ref('');
const preview = ref(null);
let inviteToken = '';

onMounted(async () => {
  inviteToken = route.query.token || new URLSearchParams(window.location.search).get('token');
  if (!inviteToken) {
    error.value = 'Invalid invite link';
    loading.value = false;
    return;
  }

  if (!isAuthenticated.value) {
    router.replace({ path: '/login', query: { invite: inviteToken } });
    return;
  }

  try {
    preview.value = await apiGet(`/rooms/invite/${inviteToken}`);
    if (route.query.join === '1') await joinInvite();
  } catch (err) {
    error.value = err.detail || 'Invite link is invalid or expired';
  } finally {
    loading.value = false;
  }
});

async function joinInvite() {
  if (admissionBusy.value) return;
  try {
    await requestJoin(preview.value.room_id, `/rooms/invite/${inviteToken}/join`);
  } catch (err) {
    error.value = err.detail || 'Failed to join';
  }
}
</script>
