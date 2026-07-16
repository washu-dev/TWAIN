import React, { useState } from 'react';
import {
  View,
  ScrollView,
  StyleSheet,
  ActivityIndicator,
  Platform,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Header, Footer, TileButton, MessageModal } from '@/components';
import { APP_STRINGS, Spacing } from '@/constants/theme';
import { apiClient } from '@/api/client';
import { useAuth } from '@/auth/AuthProvider';

export const HomeScreen: React.FC = () => {
  const { isAuthenticated, account, login, logout } = useAuth();
  const [loading, setLoading] = useState(false);
  const [modalVisible, setModalVisible] = useState(false);
  const [modalTitle, setModalTitle] = useState('');
  const [modalMessages, setModalMessages] = useState<string[]>([]);

  const showModal = (title: string, messages: string[]) => {
    setModalTitle(title);
    setModalMessages(messages);
    setModalVisible(true);
  };

  const handleTestPress = async () => {
    setLoading(true);
    try {
      const response = await apiClient.getGreetings();
      const items: { message: string }[] = response?.data ?? [];
      const messages = items.map((item) => item.message);
      showModal('Greetings from API', messages);
    } catch (error) {
      const msg = error instanceof Error ? error.message : 'Unknown error';
      showModal('Error', [msg]);
    } finally {
      setLoading(false);
    }
  };

  const handleLoginPress = async () => {
    try {
      await login();
    } catch (error) {
      const msg = error instanceof Error ? error.message : 'Unknown error';
      showModal('Sign in unavailable', [msg]);
    }
  };

  const handleLogoutPress = async () => {
    try {
      await logout();
    } catch (error) {
      const msg = error instanceof Error ? error.message : 'Unknown error';
      showModal('Sign out failed', [msg]);
    }
  };

  const userName = account?.name ?? account?.username ?? undefined;

  return (
    <SafeAreaView style={styles.container} edges={['left', 'right', 'bottom']}>
      <Header
        onTestPress={handleTestPress}
        onLoginPress={handleLoginPress}
        onLogoutPress={handleLogoutPress}
        isAuthenticated={isAuthenticated}
        userName={userName}
      />

      <ScrollView
        style={styles.scroll}
        contentContainerStyle={styles.content}
        showsVerticalScrollIndicator={Platform.OS !== 'web'}
      >
        <TileButton
          title={APP_STRINGS.startSimulation}
          description={APP_STRINGS.startSimulationDesc}
          accentColor="#BA0C2F"
          onPress={() => showModal(APP_STRINGS.startSimulation, ['Coming soon.'])}
        />

        <TileButton
          title={APP_STRINGS.resumeWorkflow}
          description={APP_STRINGS.resumeWorkflowDesc}
          accentColor="#215732"
          onPress={() => showModal(APP_STRINGS.resumeWorkflow, ['Coming soon.'])}
        />

        <TileButton
          title={APP_STRINGS.browse}
          description={APP_STRINGS.browseDesc}
          accentColor="#BA0C2F"
          onPress={() => showModal(APP_STRINGS.browse, ['Coming soon.'])}
        />

        {loading && (
          <View style={styles.loading}>
            <ActivityIndicator size="large" color="#BA0C2F" accessibilityLabel="Loading" />
          </View>
        )}
      </ScrollView>

      <Footer />

      <MessageModal
        visible={modalVisible}
        title={modalTitle}
        messages={modalMessages}
        onClose={() => setModalVisible(false)}
      />
    </SafeAreaView>
  );
};

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#F5F5F5',
  },
  scroll: {
    flex: 1,
  },
  content: {
    flexGrow: 1,
    paddingHorizontal: Spacing.four,
    paddingTop: Spacing.four,
    paddingBottom: Spacing.four,
  },
  loading: {
    alignItems: 'center',
    paddingVertical: Spacing.four,
  },
});
