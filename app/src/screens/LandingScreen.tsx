import React from 'react';
import {
  View,
  Text,
  ScrollView,
  StyleSheet,
  TouchableOpacity,
  Platform,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { Footer, WashUShield } from '@/components';
import {
  APP_STRINGS,
  Colors,
  LANDING_CONTENT,
  MaxContentWidth,
  Spacing,
} from '@/constants/theme';

/**
 * Public home page shown to signed-out visitors (app/index.tsx). Describes what
 * TWAIN is and its purpose; the only path forward into the app is signing in, so
 * every call to action routes to /login. Once authenticated, index redirects to
 * the dashboard and this screen is never shown.
 */
export const LandingScreen: React.FC = () => {
  const router = useRouter();
  const goToLogin = () => router.push('/login');

  return (
    <SafeAreaView style={styles.container} edges={['top', 'left', 'right', 'bottom']}>
      <ScrollView
        style={styles.scroll}
        contentContainerStyle={styles.scrollContent}
        showsVerticalScrollIndicator={Platform.OS !== 'web'}
      >
        {/* Hero */}
        <View style={styles.hero}>
          <View style={styles.heroInner}>
            <WashUShield height={64} variant="white" />
            <Text style={styles.heroTitle} accessibilityRole="header">
              {APP_STRINGS.appTitle}
            </Text>
            <Text style={styles.heroTagline}>{LANDING_CONTENT.tagline}</Text>
            <Text style={styles.heroLead}>{LANDING_CONTENT.heroLead}</Text>

            <View style={styles.example}>
              <Text style={styles.exampleLabel}>{LANDING_CONTENT.exampleLabel}</Text>
              <Text style={styles.exampleQuote}>{LANDING_CONTENT.exampleQuote}</Text>
            </View>

            <TouchableOpacity
              style={styles.heroCta}
              onPress={goToLogin}
              accessibilityRole="button"
              accessibilityLabel={LANDING_CONTENT.heroCta}
            >
              <Text style={styles.heroCtaText}>{LANDING_CONTENT.heroCta}</Text>
            </TouchableOpacity>
          </View>
        </View>

        {/* Body */}
        <View style={styles.body}>
          {/* What is TWAIN */}
          <View style={styles.section}>
            <Text style={styles.sectionHeading} accessibilityRole="header">
              {LANDING_CONTENT.whatHeading}
            </Text>
            <Text style={styles.paragraph}>{LANDING_CONTENT.whatBody}</Text>
          </View>

          {/* How it works */}
          <View style={styles.section}>
            <Text style={styles.sectionHeading} accessibilityRole="header">
              {LANDING_CONTENT.howHeading}
            </Text>
            <View style={styles.steps}>
              {LANDING_CONTENT.steps.map((step, i) => (
                <View key={step.title} style={styles.step}>
                  <View style={styles.stepNumber}>
                    <Text style={styles.stepNumberText}>{i + 1}</Text>
                  </View>
                  <View style={styles.stepContent}>
                    <Text style={styles.stepTitle}>{step.title}</Text>
                    <Text style={styles.stepBody}>{step.body}</Text>
                  </View>
                </View>
              ))}
            </View>
          </View>

          {/* Capabilities */}
          <View style={styles.section}>
            <Text style={styles.sectionHeading} accessibilityRole="header">
              {LANDING_CONTENT.capabilitiesHeading}
            </Text>
            <View style={styles.cards}>
              {LANDING_CONTENT.capabilities.map((cap) => (
                <View key={cap.title} style={styles.card}>
                  <View style={styles.cardAccent} />
                  <Text style={styles.cardTitle}>{cap.title}</Text>
                  <Text style={styles.cardBody}>{cap.body}</Text>
                </View>
              ))}
            </View>
          </View>

          {/* Sign-in gate */}
          <View style={styles.signInCallout}>
            <Text style={styles.signInHeading} accessibilityRole="header">
              {LANDING_CONTENT.signInHeading}
            </Text>
            <Text style={styles.signInBody}>{LANDING_CONTENT.signInBody}</Text>
            <TouchableOpacity
              style={styles.signInCta}
              onPress={goToLogin}
              accessibilityRole="button"
              accessibilityLabel={LANDING_CONTENT.signInCta}
            >
              <Text style={styles.signInCtaText}>{LANDING_CONTENT.signInCta}</Text>
            </TouchableOpacity>
          </View>
        </View>

        <Footer />
      </ScrollView>
    </SafeAreaView>
  );
};

const RED = Colors.light.washuRed;
const GREEN = Colors.light.washuGreen;

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#F5F5F5',
  },
  scroll: {
    flex: 1,
  },
  scrollContent: {
    flexGrow: 1,
  },

  // Hero
  hero: {
    backgroundColor: RED,
    borderBottomWidth: 4,
    borderBottomColor: GREEN,
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.six,
    alignItems: 'center',
  },
  heroInner: {
    width: '100%',
    maxWidth: MaxContentWidth,
    alignItems: 'center',
    gap: Spacing.three,
  },
  heroTitle: {
    fontSize: 52,
    fontWeight: 'bold',
    color: '#FFFFFF',
    letterSpacing: 1,
    textAlign: 'center',
  },
  heroTagline: {
    fontSize: 18,
    color: 'rgba(255,255,255,0.9)',
    textAlign: 'center',
    lineHeight: 26,
  },
  heroLead: {
    fontSize: 16,
    color: 'rgba(255,255,255,0.95)',
    textAlign: 'center',
    lineHeight: 24,
    maxWidth: 620,
  },
  example: {
    alignItems: 'center',
    gap: Spacing.one,
    marginTop: Spacing.one,
  },
  exampleLabel: {
    fontSize: 12,
    textTransform: 'uppercase',
    letterSpacing: 1,
    color: 'rgba(255,255,255,0.75)',
  },
  exampleQuote: {
    fontSize: 18,
    fontStyle: 'italic',
    color: '#FFFFFF',
    textAlign: 'center',
    lineHeight: 26,
  },
  heroCta: {
    marginTop: Spacing.two,
    backgroundColor: '#FFFFFF',
    borderRadius: 6,
    paddingVertical: Spacing.three,
    paddingHorizontal: Spacing.five,
  },
  heroCtaText: {
    color: RED,
    fontSize: 16,
    fontWeight: '700',
  },

  // Body
  body: {
    width: '100%',
    maxWidth: MaxContentWidth,
    alignSelf: 'center',
    paddingHorizontal: Spacing.four,
    paddingTop: Spacing.five,
    gap: Spacing.five,
  },
  section: {
    gap: Spacing.three,
  },
  sectionHeading: {
    fontSize: 24,
    fontWeight: '700',
    color: '#1A1A1A',
  },
  paragraph: {
    fontSize: 16,
    lineHeight: 25,
    color: '#3A3A3A',
  },

  // Steps
  steps: {
    gap: Spacing.three,
  },
  step: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    gap: Spacing.three,
  },
  stepNumber: {
    width: 32,
    height: 32,
    borderRadius: 16,
    backgroundColor: RED,
    alignItems: 'center',
    justifyContent: 'center',
  },
  stepNumberText: {
    color: '#FFFFFF',
    fontSize: 15,
    fontWeight: '700',
  },
  stepContent: {
    flex: 1,
    paddingTop: 2,
  },
  stepTitle: {
    fontSize: 17,
    fontWeight: '700',
    color: '#1A1A1A',
    marginBottom: Spacing.half,
  },
  stepBody: {
    fontSize: 15,
    lineHeight: 22,
    color: '#5A5A5A',
  },

  // Capability cards
  cards: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: Spacing.three,
  },
  card: {
    flexGrow: 1,
    flexBasis: '47%',
    minWidth: 240,
    backgroundColor: '#FFFFFF',
    borderRadius: 8,
    borderWidth: 1,
    borderColor: '#DDDDDD',
    padding: Spacing.three,
    gap: Spacing.two,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.06,
    shadowRadius: 4,
    elevation: 2,
  },
  cardAccent: {
    width: 36,
    height: 4,
    borderRadius: 2,
    backgroundColor: GREEN,
  },
  cardTitle: {
    fontSize: 16,
    fontWeight: '700',
    color: '#1A1A1A',
  },
  cardBody: {
    fontSize: 14,
    lineHeight: 21,
    color: '#5A5A5A',
  },

  // Sign-in callout
  signInCallout: {
    backgroundColor: '#FFFFFF',
    borderRadius: 8,
    borderTopWidth: 4,
    borderTopColor: RED,
    padding: Spacing.four,
    alignItems: 'center',
    gap: Spacing.three,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.08,
    shadowRadius: 4,
    elevation: 2,
  },
  signInHeading: {
    fontSize: 20,
    fontWeight: '700',
    color: '#1A1A1A',
    textAlign: 'center',
  },
  signInBody: {
    fontSize: 15,
    lineHeight: 22,
    color: '#5A5A5A',
    textAlign: 'center',
    maxWidth: 520,
  },
  signInCta: {
    backgroundColor: RED,
    borderRadius: 6,
    paddingVertical: Spacing.three,
    paddingHorizontal: Spacing.five,
  },
  signInCtaText: {
    color: '#FFFFFF',
    fontSize: 16,
    fontWeight: '700',
  },
});
