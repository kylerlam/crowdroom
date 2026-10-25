import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';
import AdmissionPrompt from './AdmissionPrompt.vue';

describe('applicant identity before room entry', () => {
  it.each(['pending', 'rejected'])('shows the applicant ID for %s requests', (status) => {
    const wrapper = mount(AdmissionPrompt, { props: { request: {
      room_name: 'Music room', status, message: 'Contact the room host.', public_id: 'CR-4827-9163',
    } } });
    expect(wrapper.get('.user-identity-value').text()).toBe('CR-4827-9163');
    expect(wrapper.get('[aria-label="Copy CrowdRoom ID CR-4827-9163"]').attributes('data-tooltip')).toBe('Copy ID');
    wrapper.unmount();
  });
});
