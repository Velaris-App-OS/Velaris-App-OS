"""React Native code templates.

Each template is a string with {placeholder} substitutions.
Keeps generator code readable by separating content from logic.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

PACKAGE_JSON = '''{{
  "name": "{app_slug}",
  "version": "1.0.0",
  "main": "node_modules/expo/AppEntry.js",
  "scripts": {{
    "start": "expo start",
    "android": "expo start --android",
    "ios": "expo start --ios",
    "web": "expo start --web"
  }},
  "dependencies": {{
    "expo": "~50.0.0",
    "expo-status-bar": "~1.11.1",
    "react": "18.2.0",
    "react-native": "0.73.6",
    "@react-navigation/native": "^6.1.9",
    "@react-navigation/stack": "^6.3.20",
    "@react-navigation/bottom-tabs": "^6.5.11",
    "react-native-screens": "~3.29.0",
    "react-native-safe-area-context": "4.8.2",
    "react-native-gesture-handler": "~2.14.0",
    "@react-native-async-storage/async-storage": "1.21.0"
  }},
  "devDependencies": {{
    "@babel/core": "^7.20.0"
  }},
  "private": true
}}
'''

APP_JSON = '''{{
  "expo": {{
    "name": "{app_name}",
    "slug": "{app_slug}",
    "version": "{app_version}",
    "description": "{app_description}",
    "orientation": "portrait",
    "icon": "./assets/icon.png",
    "userInterfaceStyle": "automatic",
    "splash": {{
      "image": "./assets/splash.png",
      "resizeMode": "contain",
      "backgroundColor": "{primary_color}"
    }},
    "assetBundlePatterns": ["**/*"],
    "ios": {{
      "supportsTablet": true,
      "bundleIdentifier": "{ios_bundle_id}",
      "buildNumber": "1"
    }},
    "android": {{
      "package": "{android_package}",
      "versionCode": 1,
      "adaptiveIcon": {{
        "foregroundImage": "./assets/adaptive-icon.png",
        "backgroundColor": "{primary_color}"
      }}
    }},
    "web": {{ "favicon": "./assets/favicon.png" }},
    "updates": {{
      "enabled": true,
      "fallbackToCacheTimeout": 0,
      "url": "https://u.expo.dev/your-project-id"
    }},
    "runtimeVersion": {{
      "policy": "appVersion"
    }},
    "extra": {{
      "eas": {{
        "projectId": "your-eas-project-id"
      }}
    }}
  }}
}}
'''

EAS_JSON = '''{{
  "cli": {{
    "version": ">= 7.0.0"
  }},
  "build": {{
    "development": {{
      "developmentClient": true,
      "distribution": "internal"
    }},
    "preview": {{
      "distribution": "internal",
      "android": {{ "buildType": "apk" }}
    }},
    "production": {{
      "autoIncrement": true
    }}
  }},
  "submit": {{
    "production": {{
      "ios": {{
        "appleId": "your@apple.id",
        "ascAppId": "your-app-store-connect-app-id",
        "appleTeamId": "YOUR_TEAM_ID"
      }},
      "android": {{
        "serviceAccountKeyPath": "./path/to/google-service-account.json",
        "track": "production"
      }}
    }}
  }}
}}
'''

APP_TSX = '''import React from 'react';
import {{ NavigationContainer }} from '@react-navigation/native';
import {{ createBottomTabNavigator }} from '@react-navigation/bottom-tabs';
import {{ createStackNavigator }} from '@react-navigation/stack';
import {{ StatusBar }} from 'expo-status-bar';
import {{ ApiProvider }} from './src/api/ApiContext';
import CaseListScreen from './src/screens/CaseListScreen';
import CaseDetailScreen from './src/screens/CaseDetailScreen';
import CreateCaseScreen from './src/screens/CreateCaseScreen';
import MyWorkScreen from './src/screens/MyWorkScreen';
import SettingsScreen from './src/screens/SettingsScreen';

const Tab = createBottomTabNavigator();
const Stack = createStackNavigator();

function CasesStack() {{
  return (
    <Stack.Navigator screenOptions={{{{ headerStyle: {{{{ backgroundColor: '{primary_color}' }}}}, headerTintColor: '#fff' }}}}>
      <Stack.Screen name="Cases" component={{CaseListScreen}} options={{{{ title: '{app_name}' }}}} />
      <Stack.Screen name="CaseDetail" component={{CaseDetailScreen}} options={{{{ title: 'Case Detail' }}}} />
      <Stack.Screen name="CreateCase" component={{CreateCaseScreen}} options={{{{ title: 'New Case' }}}} />
    </Stack.Navigator>
  );
}}

export default function App() {{
  return (
    <ApiProvider>
      <NavigationContainer>
        <StatusBar style="light" />
        <Tab.Navigator screenOptions={{{{
          tabBarActiveTintColor: '{primary_color}',
          headerShown: false,
        }}}}>
          <Tab.Screen name="CasesTab" component={{CasesStack}} options={{{{ title: 'Cases' }}}} />
          <Tab.Screen name="MyWork" component={{MyWorkScreen}} />
          <Tab.Screen name="Settings" component={{SettingsScreen}} />
        </Tab.Navigator>
      </NavigationContainer>
    </ApiProvider>
  );
}}
'''

API_CONTEXT = '''import React, {{ createContext, useContext, useState, useEffect }} from 'react';
import AsyncStorage from '@react-native-async-storage/async-storage';

interface ApiState {{
  baseUrl: string;
  tenantSlug: string;
  token: string;
  setBaseUrl: (url: string) => void;
  setTenantSlug: (slug: string) => void;
  setToken: (token: string) => void;
  fetch: (path: string, opts?: RequestInit) => Promise<any>;
}}

const ApiCtx = createContext<ApiState | null>(null);

export function useApi() {{
  const ctx = useContext(ApiCtx);
  if (!ctx) throw new Error('useApi must be inside ApiProvider');
  return ctx;
}}

export function ApiProvider({{ children }}: {{ children: React.ReactNode }}) {{
  const [baseUrl, setBaseUrlState] = useState('{default_api_url}');
  const [tenantSlug, setTenantSlugState] = useState('{default_tenant}');
  const [token, setTokenState] = useState('');

  useEffect(() => {{
    AsyncStorage.getItem('helix_base_url').then(v => v && setBaseUrlState(v));
    AsyncStorage.getItem('helix_tenant').then(v => v && setTenantSlugState(v));
    AsyncStorage.getItem('helix_token').then(v => v && setTokenState(v));
  }}, []);

  const setBaseUrl = (url: string) => {{ setBaseUrlState(url); AsyncStorage.setItem('helix_base_url', url); }};
  const setTenantSlug = (slug: string) => {{ setTenantSlugState(slug); AsyncStorage.setItem('helix_tenant', slug); }};
  const setToken = (tok: string) => {{ setTokenState(tok); AsyncStorage.setItem('helix_token', tok); }};

  const doFetch = async (path: string, opts: RequestInit = {{}}) => {{
    const headers: Record<string, string> = {{
      'Content-Type': 'application/json',
      'X-Tenant-Slug': tenantSlug,
      ...(opts.headers as Record<string, string> || {{}}),
    }};
    if (token) headers['Authorization'] = `Bearer ${{token}}`;

    const resp = await fetch(`${{baseUrl}}/api/v1${{path}}`, {{ ...opts, headers }});
    if (!resp.ok) throw new Error(`${{resp.status}} ${{resp.statusText}}`);
    const txt = await resp.text();
    return txt ? JSON.parse(txt) : null;
  }};

  return (
    <ApiCtx.Provider value={{{{ baseUrl, tenantSlug, token, setBaseUrl, setTenantSlug, setToken, fetch: doFetch }}}}>
      {{children}}
    </ApiCtx.Provider>
  );
}}
'''

CASE_LIST_SCREEN = '''import React, {{ useEffect, useState, useCallback }} from 'react';
import {{ View, Text, FlatList, TouchableOpacity, StyleSheet, RefreshControl, ActivityIndicator }} from 'react-native';
import {{ useApi }} from '../api/ApiContext';
import {{ useFocusEffect }} from '@react-navigation/native';

export default function CaseListScreen({{ navigation }}: any) {{
  const api = useApi();
  const [cases, setCases] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = async () => {{
    try {{
      const data = await api.fetch('/cases?page=1&page_size=50');
      setCases(data.items || []);
    }} catch (e: any) {{
      console.warn('Failed to load cases:', e.message);
    }} finally {{
      setLoading(false);
      setRefreshing(false);
    }}
  }};

  useFocusEffect(useCallback(() => {{ load(); }}, []));

  if (loading) return <View style={{styles.loading}}><ActivityIndicator size="large" color="{primary_color}" /></View>;

  return (
    <View style={{styles.container}}>
      <FlatList
        data={{cases}}
        keyExtractor={{(item) => item.id}}
        refreshControl={{<RefreshControl refreshing={{refreshing}} onRefresh={{() => {{ setRefreshing(true); load(); }}}} />}}
        renderItem={{({{item}}) => (
          <TouchableOpacity style={{styles.caseCard}} onPress={{() => navigation.navigate('CaseDetail', {{ caseId: item.id }})}}>
            <View style={{styles.cardHeader}}>
              <Text style={{styles.caseId}}>{{item.id.slice(0, 8)}}</Text>
              <Text style={{[styles.priority, {{ backgroundColor: priorityColor(item.priority) }}]}}>{{item.priority}}</Text>
            </View>
            <Text style={{styles.status}}>{{item.status.replace(/_/g, ' ')}}</Text>
            {{item.current_stage_id && <Text style={{styles.stage}}>{{item.current_stage_id}}</Text>}}
          </TouchableOpacity>
        )}}
        ListEmptyComponent={{<Text style={{styles.empty}}>No cases yet. Create one!</Text>}}
      />
      <TouchableOpacity style={{styles.fab}} onPress={{() => navigation.navigate('CreateCase')}}>
        <Text style={{styles.fabText}}>+</Text>
      </TouchableOpacity>
    </View>
  );
}}

function priorityColor(p: string): string {{
  return ({{ blocker: '#fc5c65', critical: '#fc5c65', high: '#f7b731',
           medium: '#4ecdc4', low: '#95a5a6' }} as any)[p] || '#95a5a6';
}}

const styles = StyleSheet.create({{
  container: {{ flex: 1, backgroundColor: '#f5f5f7' }},
  loading: {{ flex: 1, justifyContent: 'center', alignItems: 'center' }},
  caseCard: {{ backgroundColor: 'white', padding: 16, marginHorizontal: 12, marginVertical: 4,
              borderRadius: 8, shadowColor: '#000', shadowOpacity: 0.05, shadowRadius: 2 }},
  cardHeader: {{ flexDirection: 'row', justifyContent: 'space-between', marginBottom: 4 }},
  caseId: {{ fontSize: 11, color: '#888', fontFamily: 'monospace' }},
  priority: {{ fontSize: 10, color: 'white', paddingHorizontal: 8, paddingVertical: 2,
              borderRadius: 4, textTransform: 'uppercase', overflow: 'hidden' }},
  status: {{ fontSize: 16, fontWeight: '600', color: '#222', textTransform: 'capitalize' }},
  stage: {{ fontSize: 12, color: '#666', marginTop: 4 }},
  empty: {{ textAlign: 'center', marginTop: 48, color: '#888', fontSize: 14 }},
  fab: {{ position: 'absolute', right: 20, bottom: 20, width: 56, height: 56,
         borderRadius: 28, backgroundColor: '{primary_color}', justifyContent: 'center',
         alignItems: 'center', shadowColor: '#000', shadowOpacity: 0.2, shadowRadius: 4, elevation: 5 }},
  fabText: {{ color: 'white', fontSize: 28, fontWeight: '300' }},
}});
'''

CASE_DETAIL_SCREEN = '''import React, {{ useEffect, useState }} from 'react';
import {{ View, Text, ScrollView, StyleSheet, ActivityIndicator, TouchableOpacity }} from 'react-native';
import {{ useApi }} from '../api/ApiContext';

export default function CaseDetailScreen({{ route, navigation }}: any) {{
  const {{ caseId }} = route.params;
  const api = useApi();
  const [caseData, setCaseData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {{
    api.fetch(`/cases/${{caseId}}`).then(setCaseData).finally(() => setLoading(false));
  }}, [caseId]);

  const resolve = async () => {{
    await api.fetch(`/cases/${{caseId}}/resolve`, {{ method: 'POST', body: JSON.stringify({{}}) }});
    const updated = await api.fetch(`/cases/${{caseId}}`);
    setCaseData(updated);
  }};

  if (loading) return <View style={{styles.loading}}><ActivityIndicator size="large" color="{primary_color}" /></View>;
  if (!caseData) return <Text style={{styles.empty}}>Case not found</Text>;

  return (
    <ScrollView style={{styles.container}} contentContainerStyle={{{{ padding: 16 }}}}>
      <View style={{styles.card}}>
        <Text style={{styles.label}}>Case ID</Text>
        <Text style={{styles.value}}>{{caseData.id}}</Text>

        <Text style={{styles.label}}>Status</Text>
        <Text style={{styles.status}}>{{caseData.status.replace(/_/g, ' ')}}</Text>

        <Text style={{styles.label}}>Priority</Text>
        <Text style={{styles.value}}>{{caseData.priority}}</Text>

        {{caseData.current_stage_id && (
          <>
            <Text style={{styles.label}}>Current Stage</Text>
            <Text style={{styles.value}}>{{caseData.current_stage_id}}</Text>
          </>
        )}}

        <Text style={{styles.label}}>Data</Text>
        <View style={{styles.dataBox}}>
          <Text style={{styles.dataText}}>{{JSON.stringify(caseData.data, null, 2)}}</Text>
        </View>
      </View>

      {{(caseData.status === 'new' || caseData.status === 'open') && (
        <TouchableOpacity style={{styles.button}} onPress={{resolve}}>
          <Text style={{styles.buttonText}}>Resolve Case</Text>
        </TouchableOpacity>
      )}}
    </ScrollView>
  );
}}

const styles = StyleSheet.create({{
  container: {{ flex: 1, backgroundColor: '#f5f5f7' }},
  loading: {{ flex: 1, justifyContent: 'center', alignItems: 'center' }},
  card: {{ backgroundColor: 'white', padding: 16, borderRadius: 8, marginBottom: 16 }},
  label: {{ fontSize: 10, color: '#888', marginTop: 12, textTransform: 'uppercase', letterSpacing: 0.5 }},
  value: {{ fontSize: 15, color: '#222', marginTop: 2 }},
  status: {{ fontSize: 18, fontWeight: '600', color: '{primary_color}', marginTop: 2, textTransform: 'capitalize' }},
  dataBox: {{ backgroundColor: '#f0f0f0', padding: 10, borderRadius: 6, marginTop: 4 }},
  dataText: {{ fontFamily: 'monospace', fontSize: 11, color: '#222' }},
  button: {{ backgroundColor: '{primary_color}', padding: 14, borderRadius: 8, alignItems: 'center' }},
  buttonText: {{ color: 'white', fontSize: 15, fontWeight: '600' }},
  empty: {{ textAlign: 'center', marginTop: 48, color: '#888' }},
}});
'''

CREATE_CASE_SCREEN = '''import React, {{ useState, useEffect }} from 'react';
import {{ View, Text, ScrollView, StyleSheet, TextInput, TouchableOpacity, ActivityIndicator, Alert }} from 'react-native';
import {{ useApi }} from '../api/ApiContext';

export default function CreateCaseScreen({{ navigation }}: any) {{
  const api = useApi();
  const [caseTypes, setCaseTypes] = useState<any[]>([]);
  const [selectedType, setSelectedType] = useState<string>('');
  const [priority, setPriority] = useState('medium');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {{
    api.fetch('/case-types').then((d: any) => {{
      setCaseTypes(d.items || []);
      if (d.items?.[0]) setSelectedType(d.items[0].id);
    }});
  }}, []);

  const submit = async () => {{
    if (!selectedType) {{ Alert.alert('Error', 'Select a case type'); return; }}
    setSubmitting(true);
    try {{
      const result = await api.fetch('/cases', {{
        method: 'POST',
        body: JSON.stringify({{ case_type_id: selectedType, priority, data: {{}} }}),
      }});
      navigation.replace('CaseDetail', {{ caseId: result.id }});
    }} catch (e: any) {{
      Alert.alert('Failed to create case', e.message);
    }} finally {{ setSubmitting(false); }}
  };

  return (
    <ScrollView style={{styles.container}} contentContainerStyle={{{{ padding: 16 }}}}>
      <Text style={{styles.label}}>Case Type</Text>
      <View style={{styles.options}}>
        {{caseTypes.map(ct => (
          <TouchableOpacity key={{ct.id}}
            onPress={{() => setSelectedType(ct.id)}}
            style={{[styles.option, selectedType === ct.id && styles.optionSelected]}}>
            <Text style={{[styles.optionText, selectedType === ct.id && styles.optionTextSelected]}}>
              {{ct.name}}
            </Text>
          </TouchableOpacity>
        ))}}
      </View>

      <Text style={{styles.label}}>Priority</Text>
      <View style={{styles.options}}>
        {{['low', 'medium', 'high', 'critical'].map(p => (
          <TouchableOpacity key={{p}} onPress={{() => setPriority(p)}}
            style={{[styles.option, priority === p && styles.optionSelected]}}>
            <Text style={{[styles.optionText, priority === p && styles.optionTextSelected]}}>{{p}}</Text>
          </TouchableOpacity>
        ))}}
      </View>

      <TouchableOpacity style={{styles.button}} onPress={{submit}} disabled={{submitting}}>
        {{submitting ? <ActivityIndicator color="white" /> : <Text style={{styles.buttonText}}>Create Case</Text>}}
      </TouchableOpacity>
    </ScrollView>
  );
}}

const styles = StyleSheet.create({{
  container: {{ flex: 1, backgroundColor: '#f5f5f7' }},
  label: {{ fontSize: 11, color: '#888', marginTop: 16, marginBottom: 6, textTransform: 'uppercase', letterSpacing: 0.5 }},
  options: {{ flexDirection: 'row', flexWrap: 'wrap', gap: 6 }},
  option: {{ paddingHorizontal: 14, paddingVertical: 10, borderRadius: 6, backgroundColor: '#fff',
            borderWidth: 1, borderColor: '#e0e0e0' }},
  optionSelected: {{ backgroundColor: '{primary_color}', borderColor: '{primary_color}' }},
  optionText: {{ fontSize: 13, color: '#444', textTransform: 'capitalize' }},
  optionTextSelected: {{ color: 'white', fontWeight: '600' }},
  button: {{ backgroundColor: '{primary_color}', padding: 16, borderRadius: 8, alignItems: 'center', marginTop: 24 }},
  buttonText: {{ color: 'white', fontSize: 15, fontWeight: '600' }},
}});
'''

MY_WORK_SCREEN = '''import React, {{ useEffect, useState, useCallback }} from 'react';
import {{ View, Text, FlatList, StyleSheet, RefreshControl, ActivityIndicator, TouchableOpacity }} from 'react-native';
import {{ useApi }} from '../api/ApiContext';
import {{ useFocusEffect }} from '@react-navigation/native';

export default function MyWorkScreen() {{
  const api = useApi();
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = async () => {{
    try {{
      const data = await api.fetch('/my/work');
      setItems(Array.isArray(data) ? data : data.items || []);
    }} catch (e: any) {{
      console.warn('Failed to load work:', e.message);
    }} finally {{ setLoading(false); setRefreshing(false); }}
  }};

  useFocusEffect(useCallback(() => {{ load(); }}, []));

  if (loading) return <View style={{styles.loading}}><ActivityIndicator size="large" color="{primary_color}" /></View>;

  return (
    <FlatList
      data={{items}}
      keyExtractor={{(item, i) => item.id || String(i)}}
      refreshControl={{<RefreshControl refreshing={{refreshing}} onRefresh={{() => {{ setRefreshing(true); load(); }}}} />}}
      ListHeaderComponent={{<Text style={{styles.header}}>My Work</Text>}}
      ListEmptyComponent={{<Text style={{styles.empty}}>No active assignments</Text>}}
      renderItem={{({{item}}) => (
        <View style={{styles.item}}>
          <Text style={{styles.itemTitle}}>{{item.step_id || 'Task'}}</Text>
          <Text style={{styles.itemMeta}}>Case: {{(item.case_id || '').slice(0, 8)}}</Text>
        </View>
      )}}
    />
  );
}}

const styles = StyleSheet.create({{
  loading: {{ flex: 1, justifyContent: 'center', alignItems: 'center' }},
  header: {{ fontSize: 24, fontWeight: '700', padding: 16, color: '#222' }},
  item: {{ backgroundColor: 'white', padding: 14, marginHorizontal: 12, marginVertical: 4, borderRadius: 8 }},
  itemTitle: {{ fontSize: 15, fontWeight: '600', color: '#222', textTransform: 'capitalize' }},
  itemMeta: {{ fontSize: 11, color: '#888', marginTop: 2, fontFamily: 'monospace' }},
  empty: {{ textAlign: 'center', marginTop: 48, color: '#888' }},
}});
'''

SETTINGS_SCREEN = '''import React, {{ useState }} from 'react';
import {{ View, Text, TextInput, TouchableOpacity, StyleSheet, Alert, ScrollView }} from 'react-native';
import {{ useApi }} from '../api/ApiContext';

export default function SettingsScreen() {{
  const api = useApi();
  const [baseUrl, setBaseUrl] = useState(api.baseUrl);
  const [tenant, setTenant] = useState(api.tenantSlug);
  const [token, setToken] = useState(api.token);

  const save = () => {{
    api.setBaseUrl(baseUrl);
    api.setTenantSlug(tenant);
    api.setToken(token);
    Alert.alert('Saved', 'Settings updated');
  }};

  const testConnection = async () => {{
    try {{
      await api.fetch('/case-types');
      Alert.alert('Success', 'Connection OK');
    }} catch (e: any) {{
      Alert.alert('Failed', e.message);
    }}
  }};

  return (
    <ScrollView contentContainerStyle={{styles.container}}>
      <Text style={{styles.title}}>Settings</Text>

      <Text style={{styles.label}}>API Base URL</Text>
      <TextInput style={{styles.input}} value={{baseUrl}} onChangeText={{setBaseUrl}}
        autoCapitalize="none" autoCorrect={{false}} />

      <Text style={{styles.label}}>Tenant Slug</Text>
      <TextInput style={{styles.input}} value={{tenant}} onChangeText={{setTenant}}
        autoCapitalize="none" autoCorrect={{false}} />

      <Text style={{styles.label}}>Auth Token</Text>
      <TextInput style={{styles.input}} value={{token}} onChangeText={{setToken}}
        autoCapitalize="none" autoCorrect={{false}} secureTextEntry />

      <TouchableOpacity style={{styles.button}} onPress={{save}}>
        <Text style={{styles.buttonText}}>Save</Text>
      </TouchableOpacity>
      <TouchableOpacity style={{[styles.button, styles.buttonSecondary]}} onPress={{testConnection}}>
        <Text style={{[styles.buttonText, styles.buttonSecondaryText]}}>Test Connection</Text>
      </TouchableOpacity>

      <Text style={{styles.version}}>{app_name} · v1.0.0</Text>
    </ScrollView>
  );
}}

const styles = StyleSheet.create({{
  container: {{ padding: 20, paddingBottom: 40 }},
  title: {{ fontSize: 24, fontWeight: '700', color: '#222', marginBottom: 20 }},
  label: {{ fontSize: 11, color: '#888', marginTop: 12, marginBottom: 4, textTransform: 'uppercase', letterSpacing: 0.5 }},
  input: {{ backgroundColor: 'white', padding: 12, borderRadius: 6, fontSize: 14,
           borderWidth: 1, borderColor: '#e0e0e0' }},
  button: {{ backgroundColor: '{primary_color}', padding: 14, borderRadius: 8,
            alignItems: 'center', marginTop: 16 }},
  buttonSecondary: {{ backgroundColor: 'transparent', borderWidth: 1, borderColor: '{primary_color}' }},
  buttonText: {{ color: 'white', fontSize: 15, fontWeight: '600' }},
  buttonSecondaryText: {{ color: '{primary_color}' }},
  version: {{ textAlign: 'center', fontSize: 11, color: '#888', marginTop: 40 }},
}});
'''

BABEL_CONFIG = '''module.exports = function (api) {
  api.cache(true);
  return { presets: ['babel-preset-expo'] };
};
'''

TSCONFIG = '''{
  "extends": "expo/tsconfig.base",
  "compilerOptions": {
    "strict": true,
    "jsx": "react-native"
  }
}
'''

GITIGNORE = '''node_modules/
.expo/
dist/
npm-debug.*
*.jks
*.p8
*.p12
*.key
*.mobileprovision
*.orig.*
web-build/
.env
'''

README = '''# {app_name}

Auto-generated HELIX mobile app — powered by React Native + Expo.

## Setup

```bash
npm install
```

## Run

```bash
# iOS simulator
npm run ios

# Android emulator
npm run android

# Web
npm run web
```

## Configuration

Open the app and go to **Settings** tab to configure:
- **API Base URL** — your HELIX case-service endpoint
- **Tenant Slug** — which tenant to connect to
- **Auth Token** — JWT from your HELIX auth provider

## Screens

- **Cases** — list all cases, tap to view detail, FAB to create new
- **My Work** — your active assignments
- **Settings** — configure API + credentials

## Configured For

- **App Name**: {app_name}
- **Primary Color**: {primary_color}
- **Target Tenant**: {default_tenant}
- **API URL**: {default_api_url}

## Generated by HELIX

This app was generated from your case type definitions by HELIX Codegen (Phase 18).
Customize it as needed — it's yours to modify.
'''
