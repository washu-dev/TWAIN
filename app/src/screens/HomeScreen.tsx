import React, { useState } from 'react';
import {
  View,
  ScrollView,
  StyleSheet,
  ActivityIndicator,
  Platform,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { Header, Footer, TileButton, MessageModal } from '@/components';
import { APP_STRINGS, Spacing } from '@/constants/theme';
import { apiClient } from '@/api/client';
import { useAuth } from '@/hooks/useAuth';

export const HomeScreen: React.FC = () => {
  const router = useRouter();
  const { signOut } = useAuth();
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
      const { status } = await apiClient.health();
      showModal('API status', [`status: ${status}`]);
    } catch (error) {
      const msg = error instanceof Error ? error.message : 'Unknown error';
      showModal('Error', [msg]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <SafeAreaView style={styles.container} edges={['left', 'right', 'bottom']}>
      <Header
        onTestPress={handleTestPress}
        onLoginPress={signOut}
        loginLabel={APP_STRINGS.signOutButton}
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
          onPress={() => router.push('/chat')}
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
          onPress={() => router.push('/browse')}
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
