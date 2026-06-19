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
