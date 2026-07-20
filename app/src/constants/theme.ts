/**
 * WashU Theme and App Configuration
 * Colors sourced from https://marcomm.washu.edu/brand-color-palette/
 */

import { Platform } from 'react-native';

export const Colors = {
  light: {
    text: '#000000',
    background: '#ffffff',
    backgroundElement: '#F5F0F0',
    backgroundSelected: '#E8D5D5',
    textSecondary: '#5A5A5A',
    washuRed: '#BA0C2F',       // Primary — Pantone 200
    washuGreen: '#215732',     // Secondary — Pantone 350
    washuWhite: '#FFFFFF',
    washuLightGray: '#F2F2F2',
    washuDarkGray: '#333333',
  },
  dark: {
    text: '#ffffff',
    background: '#1A1A1A',
    backgroundElement: '#2A2A2A',
    backgroundSelected: '#3A3A3A',
    textSecondary: '#AAAAAA',
    washuRed: '#BA0C2F',
    washuGreen: '#215732',
    washuWhite: '#FFFFFF',
    washuLightGray: '#2A2A2A',
    washuDarkGray: '#CCCCCC',
  },
} as const;

export type ThemeColor = keyof typeof Colors.light & keyof typeof Colors.dark;

export const Fonts = Platform.select({
  ios: {
    sans: 'system-ui',
    serif: 'ui-serif',
    rounded: 'ui-rounded',
    mono: 'ui-monospace',
  },
  default: {
    sans: 'normal',
    serif: 'serif',
    rounded: 'normal',
    mono: 'monospace',
  },
  web: {
    sans: 'var(--font-display)',
    serif: 'var(--font-serif)',
    rounded: 'var(--font-rounded)',
    mono: 'var(--font-mono)',
  },
});

export const Spacing = {
  half: 2,
  one: 4,
  two: 8,
  three: 16,
  four: 24,
  five: 32,
  six: 64,
} as const;

export const BottomTabInset = Platform.select({ ios: 50, android: 80 }) ?? 0;
export const MaxContentWidth = 800;

export const API_CONFIG = {
  baseURL: process.env.EXPO_PUBLIC_API_BASE_URL || 'http://localhost:8000',
  timeout: 10000,
};

export const APP_STRINGS = {
  appTitle: 'TWAIN',
  appSubtitle: 'The WashU AI-Assisted Platform for Simulation From Narration',
  loginButton: 'Login',
  signOutButton: 'Sign out',
  loginPrompt: 'Sign in with your WashU email to continue.',
  loginBackToHome: '← Back to home',
  emailPlaceholder: 'you@wustl.edu',
  signInButton: 'Sign in',
  loginErrorGeneric: 'Sign in failed. Please try again.',
  testButton: 'Test',
  startSimulation: 'Start Simulation',
  resumeWorkflow: 'Resume Workflow',
  browse: 'Browse',
  startSimulationDesc: 'Instruct a custom simulation powered by AI',
  resumeWorkflowDesc: 'Continue a simulation started earlier',
  browseDesc: 'Review all simulations completed so far',
  copyrightText: '© 2024 Washington University in St. Louis. All rights reserved.',
  accessibilityStatement: 'Accessibility Statement',
  privacyPolicy: 'Privacy Policy',
};

/**
 * Copy for the public landing page (app/index.tsx → LandingScreen). Kept here so
 * the marketing content stays alongside the rest of the app's strings and can be
 * reviewed without digging through JSX.
 */
export const LANDING_CONTENT = {
  tagline: 'The WashU AI-Assisted Platform for Simulation From Narration',
  heroLead:
    'Turn a plain-language research request into a planned, executed, and validated computational run — with a human approval gate before anything is built or run.',
  heroCta: 'Sign in to get started',
  exampleLabel: 'For example, ask TWAIN to:',
  exampleQuote: '“Predict the aqueous solubility of aspirin at 25 °C.”',

  whatHeading: 'What is TWAIN?',
  whatBody:
    'TWAIN is a self-discovering, self-correcting agentic pipeline for computational science. You describe the result you want in natural language; TWAIN clarifies what it needs, finds suitable methods, proposes an execution plan for your approval, generates and runs the code — on your machine or WashU research computing — and validates the results against known baselines. Every run produces reproducible artifacts and an immutable audit trail.',

  howHeading: 'How it works',
  steps: [
    {
      title: 'Describe',
      body: 'Tell TWAIN what you want in plain language — no scripts, configs, or tool names required.',
    },
    {
      title: 'Clarify',
      body: 'TWAIN asks targeted questions until your request is unambiguous and well-scoped.',
    },
    {
      title: 'Plan & approve',
      body: 'It discovers suitable methods and synthesizes an execution plan with cost and resource estimates — then waits for your approval.',
    },
    {
      title: 'Build & run',
      body: 'On approval, TWAIN generates the code and configuration and executes the run locally or on WashU HPC.',
    },
    {
      title: 'Validate',
      body: 'Results are checked against literature baselines, with automatic diagnosis and correction when a run falls short.',
    },
  ],

  capabilitiesHeading: 'Built for reproducible research',
  capabilities: [
    {
      title: 'Natural-language intake',
      body: 'Start from a sentence, not a toolchain. TWAIN maps your intent to a structured, executable goal.',
    },
    {
      title: 'You stay in control',
      body: 'Approval gates at every critical decision — nothing is built or executed without your sign-off.',
    },
    {
      title: 'Runs on WashU computing',
      body: 'Execute locally or dispatch to WashU research computing (RIS) without leaving the conversation.',
    },
    {
      title: 'Reproducible & auditable',
      body: 'Every run captures its code, configuration, results, and a full provenance trail you can revisit.',
    },
  ],

  signInHeading: 'Sign in to continue',
  signInBody:
    'Access to TWAIN requires a Washington University account. Sign in with your WashU email to start, resume, or review simulations.',
  signInCta: 'Sign in',
} as const;
