import React, { useState } from 'react';
import { ScrollView, StyleSheet, Platform } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import {
  AmbientBackdrop, Footer, Header, IconTile, IssueModal, Reveal, TileButton,
} from '@/components';
import { APP_STRINGS, Colors, Spacing, TileAccent } from '@/constants/theme';
import { apiClient } from '@/api/client';
import { useAuth } from '@/hooks/useAuth';

const C = Colors.light;

export const DashboardScreen: React.FC = () => {
  const router = useRouter();
  const { signOut, user } = useAuth();
  const [issueModalVisible, setIssueModalVisible] = useState(false);

  return (
    <SafeAreaView style={styles.container} edges={['left', 'right', 'bottom']}>
      {/* No API-health button here any more: it is a developer check, and it sat
          in the most valuable space on the screen, beside Sign out. It now lives
          in Settings, where a researcher looks when something seems wrong. */}
      <Header onLoginPress={signOut} loginLabel={APP_STRINGS.signOutButton} />

      {/* Behind the scroll, not inside it: the wash belongs to the screen, so it
          must not slide away when the content moves. */}
      <AmbientBackdrop />

      <ScrollView
        style={styles.scroll}
        contentContainerStyle={styles.content}
        showsVerticalScrollIndicator={Platform.OS !== 'web'}
      >
        {/* One thing this screen is for. TileAccent.spends: it starts a real
            calculation on the cluster. Reveal index 0 -- it arrives first, which
            is also the reading order. */}
        <Reveal index={0}>
          <TileButton
            variant="hero"
            title={APP_STRINGS.startSimulation}
            description={APP_STRINGS.startSimulationDesc}
            accentColor={TileAccent.spends}
            onPress={() => router.push('/chat')}
          />
        </Reveal>

        <Reveal index={1}>
          <TileButton
            title={APP_STRINGS.browse}
            description={APP_STRINGS.browseDesc}
            accentColor={TileAccent.reads}
            onPress={() => router.push('/browse')}
          />
        </Reveal>

        <Reveal index={2}>
          <TileButton
            title={APP_STRINGS.tutorial}
            description={APP_STRINGS.tutorialDesc}
            accentColor={TileAccent.reads}
            onPress={() => router.push('/tutorial')}
          />
        </Reveal>

        {/* The minor three, at the weight they deserve: reachable in one tap,
            visibly not the point of the screen. Reporting an issue keeps a label
            rather than a stripe here, but it is still the one that leaves TWAIN
            (it posts publicly to GitHub), which the confirmation dialog states. */}
        <Reveal index={3} style={styles.minorRow}>
          <IconTile
            label={APP_STRINGS.libraries}
            glyph="▦"
            hint={APP_STRINGS.librariesDesc}
            onPress={() => router.push('/libraries')}
          />
          <IconTile
            label={APP_STRINGS.createIssue}
            glyph="⚑︎"
            hint={APP_STRINGS.createIssueDesc}
            onPress={() => setIssueModalVisible(true)}
          />
          <IconTile
            label={APP_STRINGS.settings}
            glyph="⚙︎"
            hint={APP_STRINGS.settingsDesc}
            onPress={() => router.push('/settings')}
          />
        </Reveal>
      </ScrollView>

      <Footer />

      <IssueModal
        visible={issueModalVisible}
        submitterEmail={user?.email}
        onSubmit={(title, body) => apiClient.createIssue(title, body)}
        onClose={() => setIssueModalVisible(false)}
      />
    </SafeAreaView>
  );
};

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: C.canvas,
  },
  scroll: {
    flex: 1,
  },
  content: {
    flexGrow: 1,
    // Centred vertically: with only three groups the leftover space all collected
    // at the bottom, which read as a page that had failed to load the rest of
    // itself. flexGrow only ever expands, so on a short screen this still scrolls
    // from the top rather than clipping the first tile.
    justifyContent: 'center',
    paddingHorizontal: Spacing.four,
    paddingTop: Spacing.four,
    paddingBottom: Spacing.four,
    // Matches SettingsScreen. Without it the tiles ran the full width of a
    // desktop window, which turned the three minor ones into wide, squat bars.
    maxWidth: 720,
    width: '100%',
    alignSelf: 'center',
  },
  // Three across, which is the most that stays legible and tappable at 375pt.
  minorRow: {
    flexDirection: 'row',
    gap: Spacing.two,
  },
});
