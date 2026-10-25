<template>
  <div class="page">
    <header class="app-header">
      <button class="btn btn-ghost" @click="router.push('/rooms')">
        <i class="ph ph-arrow-left"></i> Back
      </button>
      <h2>Profile</h2>
      <div class="header-actions">
        <ThemeToggle />
      </div>
    </header>

    <main class="container profile-container">
      <!-- Avatar & Username -->
      <section class="panel profile-header-panel">
        <div class="profile-avatar-section">
          <button
            type="button"
            class="profile-avatar-wrapper"
            aria-label="Change avatar"
            @click="$refs.avatarInput.click()"
          >
            <img v-if="avatarUrl" :src="avatarUrl" class="profile-avatar" alt="avatar" />
            <span v-else class="profile-avatar profile-avatar-fallback">{{
              (username || '?')[0].toUpperCase()
            }}</span>
            <div class="avatar-overlay"><i class="ph ph-camera"></i></div>
          </button>
          <input
            ref="avatarInput"
            type="file"
            accept="image/jpeg,image/png,image/webp"
            hidden
            @change="uploadAvatar"
          />
        </div>
        <h2 class="profile-username">{{ username }}</h2>
      </section>

      <!-- Password Section -->
      <section class="panel">
        <h3><i class="ph ph-lock"></i> Password</h3>
        <form class="profile-form" @submit.prevent="changePassword">
          <div class="input-group">
            <label for="current-password">Current password</label>
            <input
              id="current-password"
              v-model="currentPassword"
              type="password"
              class="input"
              placeholder="Current password"
              required
            />
          </div>
          <div class="input-group">
            <label for="new-password">New password</label>
            <input
              id="new-password"
              v-model="newPassword"
              type="password"
              class="input"
              placeholder="Min 6 characters"
              minlength="6"
              required
            />
          </div>
          <div class="input-group">
            <label for="confirm-password">Confirm new password</label>
            <input
              id="confirm-password"
              v-model="confirmPassword"
              type="password"
              class="input"
              placeholder="Repeat password"
              minlength="6"
              required
            />
          </div>
          <p v-if="passwordError" class="error-text">{{ passwordError }}</p>
          <p v-if="passwordSuccess" class="success-text">{{ passwordSuccess }}</p>
          <button type="submit" class="btn btn-primary" :disabled="passwordLoading">
            Change Password
          </button>
        </form>
      </section>

    </main>
  </div>
</template>

<script setup>
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { apiPost } from '../composables/useApi.js';
import { useAuth } from '../composables/useAuth.js';
import { useToast } from '../composables/useToast.js';
import ThemeToggle from '../components/ThemeToggle.vue';

const router = useRouter();
const { username, avatarUrl, setAvatarUrl } = useAuth();
const { showToast } = useToast();

const currentPassword = ref('');
const newPassword = ref('');
const confirmPassword = ref('');
const passwordError = ref('');
const passwordSuccess = ref('');
const passwordLoading = ref(false);

async function changePassword() {
  passwordError.value = '';
  passwordSuccess.value = '';

  if (newPassword.value !== confirmPassword.value) {
    passwordError.value = 'Passwords do not match';
    return;
  }

  passwordLoading.value = true;
  try {
    await apiPost('/auth/change-password', {
      current_password: currentPassword.value,
      new_password: newPassword.value,
    });
    passwordSuccess.value = 'Password changed';
    currentPassword.value = '';
    newPassword.value = '';
    confirmPassword.value = '';
  } catch (err) {
    passwordError.value = err.detail || 'Failed to change password';
  } finally {
    passwordLoading.value = false;
  }
}


async function uploadAvatar(event) {
  const file = event.target.files[0];
  if (!file) return;

  const formData = new FormData();
  formData.append('file', file);

  try {
    const user = await apiPost('/auth/avatar', formData);
    if (!user) return;
    setAvatarUrl(user.avatar_url);
    showToast('Avatar updated');
  } catch (err) {
    showToast(err.detail || 'Upload failed');
  }
}

</script>

<style scoped>
.profile-container {
  max-width: 600px;
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
  padding: var(--space-5) var(--space-4);
}

.profile-header-panel {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-5);
}

.profile-avatar-wrapper {
  position: relative;
  cursor: pointer;
  border-radius: 50%;
  overflow: hidden;
  width: 96px;
  height: 96px;
  padding: 0;
  border: none;
  background: none;
  display: block;
}

.profile-avatar-wrapper:focus-visible {
  outline: 2px solid var(--accent, #1db954);
  outline-offset: 2px;
}

.profile-avatar {
  width: 96px;
  height: 96px;
  border-radius: 50%;
  object-fit: cover;
}

.profile-avatar-fallback {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--surface-3);
  color: var(--text-1);
  font-size: 2.5rem;
  font-weight: 600;
}

.avatar-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.5);
  opacity: 0;
  transition: opacity 0.2s;
  font-size: 1.5rem;
  color: white;
}

.profile-avatar-wrapper:hover .avatar-overlay {
  opacity: 1;
}

.profile-username {
  font-size: 1.5rem;
  font-weight: 600;
  margin: 0;
}

.profile-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.success-text {
  color: var(--success);
  font-size: 0.9rem;
}

</style>
