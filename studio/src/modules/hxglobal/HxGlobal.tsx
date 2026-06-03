/**
 * P35 — HxGlobal: Multi-Region & Data Sovereignty
 * Tabs: Regions · Sovereignty Rules · Tenant Assignments · Health · Access Log
 */
import React, { useState, useEffect, useCallback } from "react";

const API = "/api/v1/global";
function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}
function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
}

type Region = {
  id: string; name: string; provider: string; location: string | null;
  endpoint: string | null; is_primary: boolean; enabled: boolean;
};
type SovRule = {
  id: string; tenant_id: string | null; case_type_id: string | null;
  region_id: string; regulation: string; description: string | null;
};
type Assignment = {
  id: string; tenant_id: string; region_id: string;
  assignment_type: string; migrated_at: string | null;
};
type HealthItem = {
  region_id: string; region_name: string; status: string;
  latency_ms: number; replication_lag_ms: number | null; error_msg: string | null;
};
type AccessLog = {
  id: string; region_id: string; tenant_id: string | null; actor_id: string | null;
  action: string; resource: string | null; legal_basis: string | null;
  recorded_at: string | null;
};
type Tenant   = { id: string; name: string };
type CaseType = { id: string; name: string };

const PROVIDERS    = ["local", "aws", "gcp", "azure", "alibaba", "oracle", "ibm", "hetzner", "ovh", "digitalocean", "linode", "on-premises", "other"];
const REGULATIONS  = ["GDPR", "HIPAA", "CCPA", "PDPA", "SOC2"];
const ASSIGN_TYPES = ["primary", "replica", "readonly"];

// Well-known cloud regions — used to auto-populate the sovereignty rules picker
// Value format: "provider|name|location"  (parsed on select to auto-create if needed)
const WORLD_REGIONS: { group: string; provider: string; name: string; location: string }[] = [
  // ── AWS ──────────────────────────────────────────────────────────────────────
  { group: "AWS — US",      provider: "aws", name: "us-east-1",      location: "N. Virginia, US" },
  { group: "AWS — US",      provider: "aws", name: "us-east-2",      location: "Ohio, US" },
  { group: "AWS — US",      provider: "aws", name: "us-west-1",      location: "N. California, US" },
  { group: "AWS — US",      provider: "aws", name: "us-west-2",      location: "Oregon, US" },
  { group: "AWS — EU",      provider: "aws", name: "eu-west-1",      location: "Ireland" },
  { group: "AWS — EU",      provider: "aws", name: "eu-west-2",      location: "London, UK" },
  { group: "AWS — EU",      provider: "aws", name: "eu-west-3",      location: "Paris, France" },
  { group: "AWS — EU",      provider: "aws", name: "eu-central-1",   location: "Frankfurt, Germany" },
  { group: "AWS — EU",      provider: "aws", name: "eu-central-2",   location: "Zurich, Switzerland" },
  { group: "AWS — EU",      provider: "aws", name: "eu-north-1",     location: "Stockholm, Sweden" },
  { group: "AWS — EU",      provider: "aws", name: "eu-south-1",     location: "Milan, Italy" },
  { group: "AWS — EU",      provider: "aws", name: "eu-south-2",     location: "Spain" },
  { group: "AWS — APAC",    provider: "aws", name: "ap-east-1",      location: "Hong Kong" },
  { group: "AWS — APAC",    provider: "aws", name: "ap-south-1",     location: "Mumbai, India" },
  { group: "AWS — APAC",    provider: "aws", name: "ap-south-2",     location: "Hyderabad, India" },
  { group: "AWS — APAC",    provider: "aws", name: "ap-southeast-1", location: "Singapore" },
  { group: "AWS — APAC",    provider: "aws", name: "ap-southeast-2", location: "Sydney, Australia" },
  { group: "AWS — APAC",    provider: "aws", name: "ap-southeast-3", location: "Jakarta, Indonesia" },
  { group: "AWS — APAC",    provider: "aws", name: "ap-southeast-4", location: "Melbourne, Australia" },
  { group: "AWS — APAC",    provider: "aws", name: "ap-northeast-1", location: "Tokyo, Japan" },
  { group: "AWS — APAC",    provider: "aws", name: "ap-northeast-2", location: "Seoul, South Korea" },
  { group: "AWS — APAC",    provider: "aws", name: "ap-northeast-3", location: "Osaka, Japan" },
  { group: "AWS — Canada",  provider: "aws", name: "ca-central-1",   location: "Montreal, Canada" },
  { group: "AWS — Canada",  provider: "aws", name: "ca-west-1",      location: "Calgary, Canada" },
  { group: "AWS — LATAM",   provider: "aws", name: "sa-east-1",      location: "São Paulo, Brazil" },
  { group: "AWS — Middle East", provider: "aws", name: "me-central-1", location: "UAE" },
  { group: "AWS — Middle East", provider: "aws", name: "me-south-1",   location: "Bahrain" },
  { group: "AWS — Africa",  provider: "aws", name: "af-south-1",     location: "Cape Town, South Africa" },
  // ── GCP ──────────────────────────────────────────────────────────────────────
  { group: "GCP — US",      provider: "gcp", name: "us-central1",        location: "Iowa, US" },
  { group: "GCP — US",      provider: "gcp", name: "us-east1",           location: "South Carolina, US" },
  { group: "GCP — US",      provider: "gcp", name: "us-east4",           location: "N. Virginia, US" },
  { group: "GCP — US",      provider: "gcp", name: "us-east5",           location: "Columbus, US" },
  { group: "GCP — US",      provider: "gcp", name: "us-south1",          location: "Dallas, US" },
  { group: "GCP — US",      provider: "gcp", name: "us-west1",           location: "Oregon, US" },
  { group: "GCP — US",      provider: "gcp", name: "us-west2",           location: "Los Angeles, US" },
  { group: "GCP — US",      provider: "gcp", name: "us-west3",           location: "Salt Lake City, US" },
  { group: "GCP — US",      provider: "gcp", name: "us-west4",           location: "Las Vegas, US" },
  { group: "GCP — EU",      provider: "gcp", name: "europe-west1",       location: "Belgium" },
  { group: "GCP — EU",      provider: "gcp", name: "europe-west2",       location: "London, UK" },
  { group: "GCP — EU",      provider: "gcp", name: "europe-west3",       location: "Frankfurt, Germany" },
  { group: "GCP — EU",      provider: "gcp", name: "europe-west4",       location: "Netherlands" },
  { group: "GCP — EU",      provider: "gcp", name: "europe-west6",       location: "Zurich, Switzerland" },
  { group: "GCP — EU",      provider: "gcp", name: "europe-west8",       location: "Milan, Italy" },
  { group: "GCP — EU",      provider: "gcp", name: "europe-west9",       location: "Paris, France" },
  { group: "GCP — EU",      provider: "gcp", name: "europe-west10",      location: "Berlin, Germany" },
  { group: "GCP — EU",      provider: "gcp", name: "europe-west12",      location: "Turin, Italy" },
  { group: "GCP — EU",      provider: "gcp", name: "europe-north1",      location: "Finland" },
  { group: "GCP — EU",      provider: "gcp", name: "europe-central2",    location: "Warsaw, Poland" },
  { group: "GCP — EU",      provider: "gcp", name: "europe-southwest1",  location: "Madrid, Spain" },
  { group: "GCP — APAC",    provider: "gcp", name: "asia-east1",         location: "Taiwan" },
  { group: "GCP — APAC",    provider: "gcp", name: "asia-east2",         location: "Hong Kong" },
  { group: "GCP — APAC",    provider: "gcp", name: "asia-northeast1",    location: "Tokyo, Japan" },
  { group: "GCP — APAC",    provider: "gcp", name: "asia-northeast2",    location: "Osaka, Japan" },
  { group: "GCP — APAC",    provider: "gcp", name: "asia-northeast3",    location: "Seoul, South Korea" },
  { group: "GCP — APAC",    provider: "gcp", name: "asia-south1",        location: "Mumbai, India" },
  { group: "GCP — APAC",    provider: "gcp", name: "asia-south2",        location: "Delhi, India" },
  { group: "GCP — APAC",    provider: "gcp", name: "asia-southeast1",    location: "Singapore" },
  { group: "GCP — APAC",    provider: "gcp", name: "asia-southeast2",    location: "Jakarta, Indonesia" },
  { group: "GCP — APAC",    provider: "gcp", name: "australia-southeast1", location: "Sydney, Australia" },
  { group: "GCP — APAC",    provider: "gcp", name: "australia-southeast2", location: "Melbourne, Australia" },
  { group: "GCP — Canada",  provider: "gcp", name: "northamerica-northeast1", location: "Montréal, Canada" },
  { group: "GCP — Canada",  provider: "gcp", name: "northamerica-northeast2", location: "Toronto, Canada" },
  { group: "GCP — LATAM",   provider: "gcp", name: "southamerica-east1", location: "São Paulo, Brazil" },
  { group: "GCP — LATAM",   provider: "gcp", name: "southamerica-west1", location: "Santiago, Chile" },
  { group: "GCP — Middle East", provider: "gcp", name: "me-central1",   location: "Doha, Qatar" },
  { group: "GCP — Middle East", provider: "gcp", name: "me-west1",      location: "Tel Aviv, Israel" },
  { group: "GCP — Africa",  provider: "gcp", name: "africa-south1",     location: "Johannesburg, South Africa" },
  // ── Azure ─────────────────────────────────────────────────────────────────────
  { group: "Azure — US",    provider: "azure", name: "eastus",              location: "Virginia, US" },
  { group: "Azure — US",    provider: "azure", name: "eastus2",             location: "Virginia, US" },
  { group: "Azure — US",    provider: "azure", name: "westus",              location: "California, US" },
  { group: "Azure — US",    provider: "azure", name: "westus2",             location: "Washington, US" },
  { group: "Azure — US",    provider: "azure", name: "westus3",             location: "Phoenix, US" },
  { group: "Azure — US",    provider: "azure", name: "centralus",           location: "Iowa, US" },
  { group: "Azure — US",    provider: "azure", name: "northcentralus",      location: "Illinois, US" },
  { group: "Azure — US",    provider: "azure", name: "southcentralus",      location: "Texas, US" },
  { group: "Azure — US",    provider: "azure", name: "westcentralus",       location: "Wyoming, US" },
  { group: "Azure — EU",    provider: "azure", name: "northeurope",         location: "Ireland" },
  { group: "Azure — EU",    provider: "azure", name: "westeurope",          location: "Netherlands" },
  { group: "Azure — EU",    provider: "azure", name: "uksouth",             location: "London, UK" },
  { group: "Azure — EU",    provider: "azure", name: "ukwest",              location: "Cardiff, UK" },
  { group: "Azure — EU",    provider: "azure", name: "germanywestcentral",  location: "Frankfurt, Germany" },
  { group: "Azure — EU",    provider: "azure", name: "germanynorth",        location: "Berlin, Germany" },
  { group: "Azure — EU",    provider: "azure", name: "francecentral",       location: "Paris, France" },
  { group: "Azure — EU",    provider: "azure", name: "francesouth",         location: "Marseille, France" },
  { group: "Azure — EU",    provider: "azure", name: "switzerlandnorth",    location: "Zurich, Switzerland" },
  { group: "Azure — EU",    provider: "azure", name: "switzerlandwest",     location: "Geneva, Switzerland" },
  { group: "Azure — EU",    provider: "azure", name: "norwayeast",          location: "Oslo, Norway" },
  { group: "Azure — EU",    provider: "azure", name: "swedencentral",       location: "Gävle, Sweden" },
  { group: "Azure — EU",    provider: "azure", name: "polandcentral",       location: "Warsaw, Poland" },
  { group: "Azure — EU",    provider: "azure", name: "italynorth",          location: "Milan, Italy" },
  { group: "Azure — EU",    provider: "azure", name: "spaincentral",        location: "Madrid, Spain" },
  { group: "Azure — APAC",  provider: "azure", name: "eastasia",            location: "Hong Kong" },
  { group: "Azure — APAC",  provider: "azure", name: "southeastasia",       location: "Singapore" },
  { group: "Azure — APAC",  provider: "azure", name: "japaneast",           location: "Tokyo, Japan" },
  { group: "Azure — APAC",  provider: "azure", name: "japanwest",           location: "Osaka, Japan" },
  { group: "Azure — APAC",  provider: "azure", name: "koreacentral",        location: "Seoul, South Korea" },
  { group: "Azure — APAC",  provider: "azure", name: "koreasouth",          location: "Busan, South Korea" },
  { group: "Azure — APAC",  provider: "azure", name: "centralindia",        location: "Pune, India" },
  { group: "Azure — APAC",  provider: "azure", name: "southindia",          location: "Chennai, India" },
  { group: "Azure — APAC",  provider: "azure", name: "westindia",           location: "Mumbai, India" },
  { group: "Azure — APAC",  provider: "azure", name: "australiaeast",       location: "Sydney, Australia" },
  { group: "Azure — APAC",  provider: "azure", name: "australiasoutheast",  location: "Melbourne, Australia" },
  { group: "Azure — APAC",  provider: "azure", name: "australiacentral",    location: "Canberra, Australia" },
  { group: "Azure — Canada", provider: "azure", name: "canadacentral",      location: "Toronto, Canada" },
  { group: "Azure — Canada", provider: "azure", name: "canadaeast",         location: "Québec City, Canada" },
  { group: "Azure — LATAM",  provider: "azure", name: "brazilsouth",        location: "São Paulo, Brazil" },
  { group: "Azure — LATAM",  provider: "azure", name: "brazilsoutheast",    location: "Rio de Janeiro, Brazil" },
  { group: "Azure — Middle East", provider: "azure", name: "uaenorth",      location: "Dubai, UAE" },
  { group: "Azure — Middle East", provider: "azure", name: "uaecentral",    location: "Abu Dhabi, UAE" },
  { group: "Azure — Middle East", provider: "azure", name: "qatarcentral",  location: "Doha, Qatar" },
  { group: "Azure — Middle East", provider: "azure", name: "israelcentral", location: "Tel Aviv, Israel" },
  { group: "Azure — Africa", provider: "azure", name: "southafricanorth",   location: "Johannesburg, South Africa" },
  { group: "Azure — Africa", provider: "azure", name: "southafricawest",    location: "Cape Town, South Africa" },
  // ── Alibaba Cloud ─────────────────────────────────────────────────────────────
  { group: "Alibaba — China",   provider: "alibaba", name: "cn-hangzhou",        location: "Hangzhou, China" },
  { group: "Alibaba — China",   provider: "alibaba", name: "cn-shanghai",        location: "Shanghai, China" },
  { group: "Alibaba — China",   provider: "alibaba", name: "cn-beijing",         location: "Beijing, China" },
  { group: "Alibaba — China",   provider: "alibaba", name: "cn-shenzhen",        location: "Shenzhen, China" },
  { group: "Alibaba — China",   provider: "alibaba", name: "cn-chengdu",         location: "Chengdu, China" },
  { group: "Alibaba — APAC",    provider: "alibaba", name: "ap-southeast-1",     location: "Singapore" },
  { group: "Alibaba — APAC",    provider: "alibaba", name: "ap-southeast-2",     location: "Sydney, Australia" },
  { group: "Alibaba — APAC",    provider: "alibaba", name: "ap-southeast-3",     location: "Kuala Lumpur, Malaysia" },
  { group: "Alibaba — APAC",    provider: "alibaba", name: "ap-southeast-5",     location: "Jakarta, Indonesia" },
  { group: "Alibaba — APAC",    provider: "alibaba", name: "ap-northeast-1",     location: "Tokyo, Japan" },
  { group: "Alibaba — APAC",    provider: "alibaba", name: "ap-south-1",         location: "Mumbai, India" },
  { group: "Alibaba — EU",      provider: "alibaba", name: "eu-central-1",       location: "Frankfurt, Germany" },
  { group: "Alibaba — EU",      provider: "alibaba", name: "eu-west-1",          location: "London, UK" },
  { group: "Alibaba — US",      provider: "alibaba", name: "us-east-1",          location: "Virginia, US" },
  { group: "Alibaba — US",      provider: "alibaba", name: "us-west-1",          location: "Silicon Valley, US" },
  { group: "Alibaba — Middle East", provider: "alibaba", name: "me-east-1",      location: "Dubai, UAE" },
  // ── Oracle Cloud ──────────────────────────────────────────────────────────────
  { group: "Oracle — US",       provider: "oracle", name: "us-ashburn-1",        location: "Ashburn, Virginia, US" },
  { group: "Oracle — US",       provider: "oracle", name: "us-phoenix-1",        location: "Phoenix, Arizona, US" },
  { group: "Oracle — US",       provider: "oracle", name: "us-sanjose-1",        location: "San Jose, California, US" },
  { group: "Oracle — US",       provider: "oracle", name: "us-chicago-1",        location: "Chicago, US" },
  { group: "Oracle — EU",       provider: "oracle", name: "eu-frankfurt-1",      location: "Frankfurt, Germany" },
  { group: "Oracle — EU",       provider: "oracle", name: "eu-amsterdam-1",      location: "Amsterdam, Netherlands" },
  { group: "Oracle — EU",       provider: "oracle", name: "eu-zurich-1",         location: "Zurich, Switzerland" },
  { group: "Oracle — EU",       provider: "oracle", name: "eu-stockholm-1",      location: "Stockholm, Sweden" },
  { group: "Oracle — EU",       provider: "oracle", name: "eu-milan-1",          location: "Milan, Italy" },
  { group: "Oracle — EU",       provider: "oracle", name: "eu-paris-1",          location: "Paris, France" },
  { group: "Oracle — EU",       provider: "oracle", name: "eu-madrid-1",         location: "Madrid, Spain" },
  { group: "Oracle — EU",       provider: "oracle", name: "uk-london-1",         location: "London, UK" },
  { group: "Oracle — EU",       provider: "oracle", name: "uk-cardiff-1",        location: "Cardiff, UK" },
  { group: "Oracle — APAC",     provider: "oracle", name: "ap-tokyo-1",          location: "Tokyo, Japan" },
  { group: "Oracle — APAC",     provider: "oracle", name: "ap-osaka-1",          location: "Osaka, Japan" },
  { group: "Oracle — APAC",     provider: "oracle", name: "ap-sydney-1",         location: "Sydney, Australia" },
  { group: "Oracle — APAC",     provider: "oracle", name: "ap-melbourne-1",      location: "Melbourne, Australia" },
  { group: "Oracle — APAC",     provider: "oracle", name: "ap-mumbai-1",         location: "Mumbai, India" },
  { group: "Oracle — APAC",     provider: "oracle", name: "ap-hyderabad-1",      location: "Hyderabad, India" },
  { group: "Oracle — APAC",     provider: "oracle", name: "ap-seoul-1",          location: "Seoul, South Korea" },
  { group: "Oracle — APAC",     provider: "oracle", name: "ap-singapore-1",      location: "Singapore" },
  { group: "Oracle — Canada",   provider: "oracle", name: "ca-toronto-1",        location: "Toronto, Canada" },
  { group: "Oracle — Canada",   provider: "oracle", name: "ca-montreal-1",       location: "Montréal, Canada" },
  { group: "Oracle — Middle East", provider: "oracle", name: "me-dubai-1",       location: "Dubai, UAE" },
  { group: "Oracle — Middle East", provider: "oracle", name: "me-abudhabi-1",    location: "Abu Dhabi, UAE" },
  { group: "Oracle — LATAM",    provider: "oracle", name: "sa-saopaulo-1",       location: "São Paulo, Brazil" },
  { group: "Oracle — LATAM",    provider: "oracle", name: "sa-vinhedo-1",        location: "Vinhedo, Brazil" },
  // ── IBM Cloud ─────────────────────────────────────────────────────────────────
  { group: "IBM — US",          provider: "ibm", name: "us-south",               location: "Dallas, US" },
  { group: "IBM — US",          provider: "ibm", name: "us-east",                location: "Washington DC, US" },
  { group: "IBM — EU",          provider: "ibm", name: "eu-de",                  location: "Frankfurt, Germany" },
  { group: "IBM — EU",          provider: "ibm", name: "eu-gb",                  location: "London, UK" },
  { group: "IBM — EU",          provider: "ibm", name: "eu-es",                  location: "Madrid, Spain" },
  { group: "IBM — APAC",        provider: "ibm", name: "jp-tok",                 location: "Tokyo, Japan" },
  { group: "IBM — APAC",        provider: "ibm", name: "jp-osa",                 location: "Osaka, Japan" },
  { group: "IBM — APAC",        provider: "ibm", name: "au-syd",                 location: "Sydney, Australia" },
  { group: "IBM — Canada",      provider: "ibm", name: "ca-tor",                 location: "Toronto, Canada" },
  { group: "IBM — LATAM",       provider: "ibm", name: "br-sao",                 location: "São Paulo, Brazil" },
  // ── Hetzner ───────────────────────────────────────────────────────────────────
  { group: "Hetzner",           provider: "hetzner", name: "eu-central-fsn1",    location: "Falkenstein, Germany" },
  { group: "Hetzner",           provider: "hetzner", name: "eu-central-nbg1",    location: "Nuremberg, Germany" },
  { group: "Hetzner",           provider: "hetzner", name: "eu-central-hel1",    location: "Helsinki, Finland" },
  { group: "Hetzner",           provider: "hetzner", name: "us-east-ash",        location: "Ashburn, Virginia, US" },
  { group: "Hetzner",           provider: "hetzner", name: "us-west-hil",        location: "Hillsboro, Oregon, US" },
  { group: "Hetzner",           provider: "hetzner", name: "ap-southeast-sin",   location: "Singapore" },
  // ── OVHcloud ──────────────────────────────────────────────────────────────────
  { group: "OVHcloud — EU",     provider: "ovh", name: "eu-west-rbx",            location: "Roubaix, France" },
  { group: "OVHcloud — EU",     provider: "ovh", name: "eu-west-sbg",            location: "Strasbourg, France" },
  { group: "OVHcloud — EU",     provider: "ovh", name: "eu-west-gra",            location: "Gravelines, France" },
  { group: "OVHcloud — EU",     provider: "ovh", name: "eu-west-lim",            location: "Limburg, Germany" },
  { group: "OVHcloud — EU",     provider: "ovh", name: "eu-west-lon",            location: "London, UK" },
  { group: "OVHcloud — EU",     provider: "ovh", name: "eu-west-waw",            location: "Warsaw, Poland" },
  { group: "OVHcloud — APAC",   provider: "ovh", name: "ap-southeast-sgp",       location: "Singapore" },
  { group: "OVHcloud — APAC",   provider: "ovh", name: "ap-southeast-syd",       location: "Sydney, Australia" },
  { group: "OVHcloud — Canada", provider: "ovh", name: "ca-east-bhs",            location: "Beauharnois, Canada" },
  { group: "OVHcloud — US",     provider: "ovh", name: "us-east-vin",            location: "Vint Hill, Virginia, US" },
  { group: "OVHcloud — US",     provider: "ovh", name: "us-west-hil",            location: "Hillsboro, Oregon, US" },
  // ── DigitalOcean ──────────────────────────────────────────────────────────────
  { group: "DigitalOcean — US", provider: "digitalocean", name: "nyc1",          location: "New York, US" },
  { group: "DigitalOcean — US", provider: "digitalocean", name: "nyc3",          location: "New York, US" },
  { group: "DigitalOcean — US", provider: "digitalocean", name: "sfo2",          location: "San Francisco, US" },
  { group: "DigitalOcean — US", provider: "digitalocean", name: "sfo3",          location: "San Francisco, US" },
  { group: "DigitalOcean — EU", provider: "digitalocean", name: "ams3",          location: "Amsterdam, Netherlands" },
  { group: "DigitalOcean — EU", provider: "digitalocean", name: "fra1",          location: "Frankfurt, Germany" },
  { group: "DigitalOcean — EU", provider: "digitalocean", name: "lon1",          location: "London, UK" },
  { group: "DigitalOcean — APAC", provider: "digitalocean", name: "sgp1",        location: "Singapore" },
  { group: "DigitalOcean — APAC", provider: "digitalocean", name: "blr1",        location: "Bangalore, India" },
  { group: "DigitalOcean — APAC", provider: "digitalocean", name: "syd1",        location: "Sydney, Australia" },
  { group: "DigitalOcean — Canada", provider: "digitalocean", name: "tor1",      location: "Toronto, Canada" },
  // ── Linode / Akamai ───────────────────────────────────────────────────────────
  { group: "Linode — US",       provider: "linode", name: "us-east",             location: "Newark, New Jersey, US" },
  { group: "Linode — US",       provider: "linode", name: "us-central",          location: "Dallas, Texas, US" },
  { group: "Linode — US",       provider: "linode", name: "us-west",             location: "Fremont, California, US" },
  { group: "Linode — US",       provider: "linode", name: "us-southeast",        location: "Atlanta, Georgia, US" },
  { group: "Linode — EU",       provider: "linode", name: "eu-west",             location: "London, UK" },
  { group: "Linode — EU",       provider: "linode", name: "eu-central",          location: "Frankfurt, Germany" },
  { group: "Linode — APAC",     provider: "linode", name: "ap-south",            location: "Mumbai, India" },
  { group: "Linode — APAC",     provider: "linode", name: "ap-west",             location: "Mumbai, India" },
  { group: "Linode — APAC",     provider: "linode", name: "ap-southeast",        location: "Singapore" },
  { group: "Linode — APAC",     provider: "linode", name: "ap-northeast",        location: "Tokyo, Japan" },
  { group: "Linode — Canada",   provider: "linode", name: "ca-central",          location: "Toronto, Canada" },
];

// Encode a well-known region into a select value
const WK_PREFIX = "wk:";
function encodeWK(r: typeof WORLD_REGIONS[0]) {
  return `${WK_PREFIX}${r.provider}|${r.name}|${r.location}`;
}
function decodeWK(v: string) {
  const [provider, name, location] = v.slice(WK_PREFIX.length).split("|");
  return { provider, name, location };
}

const STATUS_COLOR: Record<string, string> = {
  healthy: "#22c55e", degraded: "#f59e0b", unreachable: "#ef4444",
};
const PROVIDER_COLOR: Record<string, string> = {
  local: "#0d9488", aws: "#f59e0b", gcp: "#3b82f6", azure: "#0078d4",
};

const S: Record<string, React.CSSProperties> = {
  page:      { display: "flex", flexDirection: "column", height: "100%", background: "var(--bg-main)", color: "var(--text-primary)", fontFamily: "system-ui, sans-serif" },
  header:    { padding: "18px 24px 0", flexShrink: 0 },
  title:     { fontSize: 20, fontWeight: 700, margin: 0 },
  sub:       { fontSize: 13, color: "var(--text-secondary)", marginTop: 4 },
  tabBar:    { display: "flex", gap: 4, padding: "14px 24px 0", borderBottom: "1px solid var(--border)", flexShrink: 0 },
  tab:       { padding: "8px 20px", border: "none", background: "none", fontSize: 13, cursor: "pointer", borderBottom: "2px solid transparent", fontWeight: 400, color: "var(--text-secondary)" },
  tabActive: { borderBottom: "2px solid var(--accent)", color: "var(--accent)", fontWeight: 700 },
  body:      { flex: 1, overflow: "auto", padding: "20px 28px" },
  card:      { background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 8, padding: "16px 20px", marginBottom: 12 },
  btn:       { padding: "7px 16px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: 700 },
  btnP:      { background: "var(--accent)", color: "#fff" },
  btnS:      { background: "var(--bg-surface)", border: "1px solid var(--border)", color: "var(--text-secondary)" },
  btnD:      { background: "#fee2e2", color: "#ef4444", border: "1px solid #fecaca" },
  input:     { width: "100%", padding: "7px 11px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: "var(--bg-main)", color: "var(--text-primary)" },
  select:    { padding: "7px 11px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, background: "var(--bg-main)", color: "var(--text-primary)", width: "100%" },
  label:     { fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 4, display: "block" },
  badge:     { fontSize: 10, padding: "2px 8px", borderRadius: 10, fontWeight: 700 },
  tbl:       { width: "100%", borderCollapse: "collapse" as const, fontSize: 12 },
  th:        { textAlign: "left" as const, padding: "7px 10px", color: "var(--text-secondary)", fontWeight: 600, borderBottom: "1px solid var(--border)" },
  td:        { padding: "7px 10px", borderBottom: "1px solid var(--border)", verticalAlign: "middle" as const },
};

function Badge({ label, color }: { label: string; color: string }) {
  return <span style={{ ...S.badge, background: color + "22", color }}>{label}</span>;
}

function fmtDate(s: string | null) {
  return s ? new Date(s).toLocaleString() : "—";
}

function truncate(s: string, n = 48) {
  return s.length > n ? s.slice(0, n) + "…" : s;
}


// ── Confirm Delete Modal ──────────────────────────────────────────────────────

function ConfirmModal({ message, onConfirm, onCancel }: {
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.55)",
      zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center",
    }}>
      <div style={{
        background: "#fff", border: "1px solid #e2e8f0", borderRadius: 10,
        padding: "28px 32px", width: 400, boxShadow: "0 20px 60px rgba(0,0,0,0.3)",
        color: "#1a202c",
      }}>
        <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 10, color: "#1a202c" }}>Confirm Delete</div>
        <div style={{ fontSize: 13, color: "#64748b", marginBottom: 24, lineHeight: 1.6 }}>
          {message}
        </div>
        <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
          <button style={{ ...S.btn, background: "#f1f5f9", border: "1px solid #e2e8f0", color: "#475569" }} onClick={onCancel}>Cancel</button>
          <button style={{ ...S.btn, background: "#fee2e2", color: "#ef4444", border: "1px solid #fecaca" }} onClick={onConfirm}>Delete</button>
        </div>
      </div>
    </div>
  );
}


// ── Regions tab ───────────────────────────────────────────────────────────────

type EditRegionForm = { name: string; location: string; endpoint: string; config_raw: string; enabled: boolean; is_primary: boolean };

function RegionsTab() {
  const [regions, setRegions]       = useState<Region[]>([]);
  const [creating, setCreating]     = useState(false);
  const [form, setForm]             = useState({ name: "", provider: "local", location: "", config_raw: "{}" });
  const [editingId, setEditingId]   = useState<string | null>(null);
  const [editForm, setEditForm]     = useState<EditRegionForm>({ name: "", location: "", endpoint: "", config_raw: "{}", enabled: true, is_primary: false });
  const [pingResults, setPingResults] = useState<Record<string, { ok: boolean; message: string; latency_ms: number }>>({});
  const [confirmDelete, setConfirmDelete] = useState<Region | null>(null);
  const [msg, setMsg]               = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await authFetch(`${API}/regions`);
    if (r.ok) setRegions((await r.json()).regions);
  }, []);

  useEffect(() => { load(); }, [load]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    let config: Record<string, string> = {};
    try { config = JSON.parse(form.config_raw); } catch { setMsg("Invalid JSON in connection config"); return; }
    const r = await authFetch(`${API}/regions`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: form.name, provider: form.provider, location: form.location || null, connection_config: config }),
    });
    if (r.ok) { setCreating(false); setForm({ name: "", provider: "local", location: "", config_raw: "{}" }); setMsg(null); await load(); }
    else { const err = await r.json(); setMsg(err.detail ?? "Create failed"); }
  }

  async function handlePing(region: Region) {
    const r = await authFetch(`${API}/regions/${region.id}/ping`, { method: "POST" });
    if (r.ok) { const data = await r.json(); setPingResults(prev => ({ ...prev, [region.id]: data })); }
  }

  function startEdit(region: Region) {
    setEditingId(region.id);
    setEditForm({
      name: region.name, location: region.location ?? "",
      endpoint: region.endpoint ?? "",
      config_raw: JSON.stringify((region as any).connection_config ?? {}, null, 2),
      enabled: region.enabled, is_primary: region.is_primary,
    });
    setMsg(null);
  }

  async function handleEdit(e: React.FormEvent) {
    e.preventDefault();
    if (!editingId) return;
    let connection_config: Record<string, string> = {};
    try { connection_config = JSON.parse(editForm.config_raw); } catch { setMsg("Invalid JSON in connection config"); return; }
    const r = await authFetch(`${API}/regions/${editingId}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: editForm.name, location: editForm.location || null, endpoint: editForm.endpoint || null, connection_config, enabled: editForm.enabled, is_primary: editForm.is_primary }),
    });
    if (r.ok) { setEditingId(null); setMsg(null); await load(); }
    else { const err = await r.json(); setMsg(err.detail ?? "Update failed"); }
  }

  async function confirmAndDelete() {
    if (!confirmDelete) return;
    await authFetch(`${API}/regions/${confirmDelete.id}`, { method: "DELETE" });
    setConfirmDelete(null);
    await load();
  }

  return (
    <div style={S.body}>
      {confirmDelete && (
        <ConfirmModal
          message={`Delete region "${confirmDelete.name}"? Any sovereignty rules or assignments referencing it may break.`}
          onConfirm={confirmAndDelete}
          onCancel={() => setConfirmDelete(null)}
        />
      )}

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>Regions</h2>
        <button style={{ ...S.btn, ...S.btnP }} onClick={() => setCreating(v => !v)}>
          {creating ? "✕ Cancel" : "+ Add Region"}
        </button>
      </div>

      {msg && <div style={{ fontSize: 12, marginBottom: 12, color: "#ef4444" }}>{msg}</div>}

      {creating && (
        <div style={{ ...S.card, marginBottom: 20 }}>
          <form onSubmit={handleCreate}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 10 }}>
              <div>
                <label style={S.label}>Name</label>
                <input style={S.input} value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} required placeholder="eu-frankfurt" />
              </div>
              <div>
                <label style={S.label}>Provider</label>
                <select style={S.select} value={form.provider} onChange={e => setForm(f => ({ ...f, provider: e.target.value }))}>
                  {PROVIDERS.map(p => <option key={p}>{p}</option>)}
                </select>
              </div>
              <div>
                <label style={S.label}>Location (optional)</label>
                <input style={S.input} value={form.location} onChange={e => setForm(f => ({ ...f, location: e.target.value }))} placeholder="Frankfurt, Germany" />
              </div>
            </div>
            <div style={{ marginBottom: 12 }}>
              <label style={S.label}>Connection Config (JSON)</label>
              <textarea style={{ ...S.input, height: 60, resize: "vertical", fontFamily: "monospace" }}
                value={form.config_raw} onChange={e => setForm(f => ({ ...f, config_raw: e.target.value }))} />
              <div style={{ fontSize: 10, color: "var(--text-secondary)", marginTop: 4 }}>
                AWS: {`{"region":"eu-central-1","endpoint":"https://x"}`} · GCP: {`{"project_id":"my-proj"}`} · Azure: {`{"subscription_id":"sub-123"}`}
              </div>
            </div>
            <button type="submit" style={{ ...S.btn, ...S.btnP }}>Create Region</button>
          </form>
        </div>
      )}

      {regions.length === 0 && !creating
        ? <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>No regions configured. Velaris runs as a single-region deployment by default.</div>
        : regions.map(region => (
          <div key={region.id} style={S.card}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
              <span style={{ fontSize: 13, fontWeight: 700, flex: 1 }}>{region.name}</span>
              <Badge label={region.provider} color={PROVIDER_COLOR[region.provider] ?? "#888"} />
              {region.is_primary && <Badge label="PRIMARY" color="#22c55e" />}
              {!region.enabled && <Badge label="DISABLED" color="#94a3b8" />}
              <button style={{ ...S.btn, ...S.btnS }} onClick={() => handlePing(region)}>Ping</button>
              <button style={{ ...S.btn, ...S.btnS }} onClick={() => editingId === region.id ? setEditingId(null) : startEdit(region)}>
                {editingId === region.id ? "Cancel" : "Edit"}
              </button>
              <button style={{ ...S.btn, ...S.btnD }} onClick={() => setConfirmDelete(region)}>Delete</button>
            </div>
            {region.location && <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>📍 {region.location}</div>}
            {pingResults[region.id] && (
              <div style={{ marginTop: 8, fontSize: 11, color: pingResults[region.id].ok ? "#22c55e" : "#ef4444" }}>
                {pingResults[region.id].ok ? "✓" : "✗"} {pingResults[region.id].message} ({pingResults[region.id].latency_ms}ms)
              </div>
            )}
            {editingId === region.id && (
              <form onSubmit={handleEdit} style={{ marginTop: 14, paddingTop: 14, borderTop: "1px solid var(--border)" }}>
                {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 10 }}>
                  <div>
                    <label style={S.label}>Name</label>
                    <input style={S.input} value={editForm.name} onChange={e => setEditForm(f => ({ ...f, name: e.target.value }))} required />
                  </div>
                  <div>
                    <label style={S.label}>Location</label>
                    <input style={S.input} value={editForm.location} onChange={e => setEditForm(f => ({ ...f, location: e.target.value }))} placeholder="Frankfurt, Germany" />
                  </div>
                  <div>
                    <label style={S.label}>Endpoint (optional)</label>
                    <input style={S.input} value={editForm.endpoint} onChange={e => setEditForm(f => ({ ...f, endpoint: e.target.value }))} placeholder="https://region.example.com" />
                  </div>
                  <div style={{ display: "flex", gap: 16, alignItems: "center", paddingTop: 18 }}>
                    <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12, cursor: "pointer" }}>
                      <input type="checkbox" checked={editForm.enabled} onChange={e => setEditForm(f => ({ ...f, enabled: e.target.checked }))} />
                      Enabled
                    </label>
                    <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12, cursor: "pointer" }}>
                      <input type="checkbox" checked={editForm.is_primary} onChange={e => setEditForm(f => ({ ...f, is_primary: e.target.checked }))} />
                      Primary
                    </label>
                  </div>
                </div>
                <div style={{ marginBottom: 10 }}>
                  <label style={S.label}>Connection Config (JSON)</label>
                  <textarea style={{ ...S.input, height: 80, resize: "vertical", fontFamily: "monospace" }}
                    value={editForm.config_raw} onChange={e => setEditForm(f => ({ ...f, config_raw: e.target.value }))} />
                </div>
                <div style={{ display: "flex", gap: 8 }}>
                  <button type="submit" style={{ ...S.btn, ...S.btnP }}>Save Changes</button>
                  <button type="button" style={{ ...S.btn, ...S.btnS }} onClick={() => setEditingId(null)}>Cancel</button>
                </div>
              </form>
            )}
          </div>
        ))
      }
    </div>
  );
}


// ── Sovereignty Rules tab ─────────────────────────────────────────────────────

type EditRuleForm = { tenant_id: string; case_type_id: string; region_id: string; regulation: string; description: string };

// Groups the well-known list into [{group, items}] for rendering optgroups
function groupedWorldRegions() {
  const map = new Map<string, typeof WORLD_REGIONS>();
  for (const r of WORLD_REGIONS) {
    if (!map.has(r.group)) map.set(r.group, []);
    map.get(r.group)!.push(r);
  }
  return Array.from(map.entries()).map(([group, items]) => ({ group, items }));
}

const CUSTOM_SENTINEL = "__custom__";

// Renders a region <select> with configured regions at top, well-known below,
// and a "Custom / On-premises" option that reveals an inline creation form.
function RegionSelect({ value, onChange, regions, style }: {
  value: string;
  onChange: (v: string) => void;
  regions: Region[];
  style?: React.CSSProperties;
}) {
  const [showCustom, setShowCustom] = useState(false);
  const [custom, setCustom] = useState({ provider: "on-premises", name: "", location: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const groups = groupedWorldRegions();
  const configuredNames = new Set(regions.map(r => r.name));

  function handleSelectChange(v: string) {
    if (v === CUSTOM_SENTINEL) {
      setShowCustom(true);
      setErr(null);
    } else {
      setShowCustom(false);
      onChange(v);
    }
  }

  async function handleCustomAdd() {
    if (!custom.name.trim()) { setErr("Region name is required"); return; }
    setBusy(true); setErr(null);
    try {
      const r = await authFetch(`${API}/regions`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: custom.name.trim(), provider: custom.provider, location: custom.location.trim() || null, connection_config: {} }),
      });
      if (!r.ok) { const e = await r.json(); throw new Error(e.detail ?? "Failed to create region"); }
      const created: Region = await r.json();
      setShowCustom(false);
      setCustom({ provider: "on-premises", name: "", location: "" });
      onChange(created.id);
    } catch (e: any) {
      setErr(e.message);
    } finally { setBusy(false); }
  }

  const selectValue = showCustom ? CUSTOM_SENTINEL : value;

  return (
    <div>
      <select style={{ ...S.select, ...style }} value={selectValue} onChange={e => handleSelectChange(e.target.value)}>
        <option value="">— select region —</option>
        {regions.length > 0 && (
          <optgroup label="─── Configured regions ───">
            {regions.map(r => (
              <option key={r.id} value={r.id}>{r.name}{r.location ? ` · ${r.location}` : ""}</option>
            ))}
          </optgroup>
        )}
        {groups.map(({ group, items }) => {
          const available = items.filter(r => !configuredNames.has(r.name));
          if (!available.length) return null;
          return (
            <optgroup key={group} label={`─── ${group} ───`}>
              {available.map(r => (
                <option key={encodeWK(r)} value={encodeWK(r)}>
                  {r.name} · {r.location}
                </option>
              ))}
            </optgroup>
          );
        })}
        <optgroup label="─────────────────────────">
          <option value={CUSTOM_SENTINEL}>＋ Custom / On-premises…</option>
        </optgroup>
      </select>

      {showCustom && (
        <div style={{ marginTop: 8, padding: "12px 14px", border: "1px solid var(--border)", borderRadius: 6, background: "var(--bg-main)" }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.05em" }}>
            Custom / On-premises Region
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, marginBottom: 8 }}>
            <div>
              <label style={S.label}>Provider</label>
              <select style={S.select} value={custom.provider} onChange={e => setCustom(c => ({ ...c, provider: e.target.value }))}>
                {PROVIDERS.map(p => <option key={p} value={p}>{p}</option>)}
              </select>
            </div>
            <div>
              <label style={S.label}>Region name *</label>
              <input style={S.input} value={custom.name} onChange={e => setCustom(c => ({ ...c, name: e.target.value }))}
                placeholder="e.g. dc-london-1" />
            </div>
            <div>
              <label style={S.label}>Location</label>
              <input style={S.input} value={custom.location} onChange={e => setCustom(c => ({ ...c, location: e.target.value }))}
                placeholder="e.g. London, UK" />
            </div>
          </div>
          {err && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 6 }}>{err}</div>}
          <div style={{ display: "flex", gap: 6 }}>
            <button type="button" style={{ ...S.btn, ...S.btnP }} onClick={handleCustomAdd} disabled={busy}>
              {busy ? "Adding…" : "Add & Select"}
            </button>
            <button type="button" style={{ ...S.btn, ...S.btnS }} onClick={() => { setShowCustom(false); setErr(null); }}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function SovereigntyTab() {
  const [regions, setRegions]         = useState<Region[]>([]);
  const [tenants, setTenants]         = useState<Tenant[]>([]);
  const [caseTypes, setCaseTypes]     = useState<CaseType[]>([]);
  const [rules, setRules]             = useState<SovRule[]>([]);
  const [form, setForm]               = useState({ tenant_id: "", case_type_id: "", region_id: "", regulation: "GDPR", description: "" });
  const [editingId, setEditingId]     = useState<string | null>(null);
  const [editForm, setEditForm]       = useState<EditRuleForm>({ tenant_id: "", case_type_id: "", region_id: "", regulation: "GDPR", description: "" });
  const [resolveForm, setResolveForm] = useState({ tenant_id: "", case_type_id: "" });
  const [resolveResult, setResolveResult] = useState<{ region: Region | null } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<SovRule | null>(null);
  const [msg, setMsg]                 = useState<string | null>(null);

  const load = useCallback(async () => {
    const [rr, sr, tr, ctr] = await Promise.all([
      authFetch(`${API}/regions`),
      authFetch(`${API}/sovereignty-rules`),
      authFetch("/api/v1/tenants"),
      authFetch("/api/v1/case-types?page_size=200"),
    ]);
    if (rr.ok)  setRegions((await rr.json()).regions);
    if (sr.ok)  setRules((await sr.json()).rules);
    if (tr.ok)  { const d = await tr.json(); setTenants(Array.isArray(d) ? d : d.tenants ?? []); }
    if (ctr.ok) { const d = await ctr.json(); setCaseTypes(d.items ?? []); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const regionName   = (id: string) => regions.find(r => r.id === id)?.name ?? id.slice(0, 8);
  const tenantLabel  = (id: string | null) => id ? (tenants.find(t => t.id === id)?.name ?? id.slice(0, 16)) : null;
  const ctLabel      = (id: string | null) => id ? (caseTypes.find(ct => ct.id === id)?.name ?? id.slice(0, 16)) : null;

  // If the selected value is a well-known region (not yet in DB), auto-create it
  // and return the real UUID. Otherwise just return the value as-is.
  async function resolveRegionId(value: string): Promise<string | null> {
    if (!value) return null;
    if (!value.startsWith(WK_PREFIX)) return value; // already a UUID
    const { provider, name, location } = decodeWK(value);
    const r = await authFetch(`${API}/regions`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, provider, location, connection_config: {} }),
    });
    if (!r.ok) { const err = await r.json(); throw new Error(err.detail ?? "Failed to register region"); }
    const created: Region = await r.json();
    return created.id;
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!form.region_id) { setMsg("Select a region"); return; }
    let regionId: string | null;
    try { regionId = await resolveRegionId(form.region_id); }
    catch (e: any) { setMsg(e.message); return; }
    const r = await authFetch(`${API}/sovereignty-rules`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: form.tenant_id || null, case_type_id: form.case_type_id || null,
        region_id: regionId, regulation: form.regulation, description: form.description || null,
      }),
    });
    if (r.ok) { setForm({ tenant_id: "", case_type_id: "", region_id: "", regulation: "GDPR", description: "" }); setMsg(null); await load(); }
    else { const err = await r.json(); setMsg(err.detail ?? "Create failed"); }
  }

  function startEdit(rule: SovRule) {
    setEditingId(rule.id);
    setEditForm({
      tenant_id: rule.tenant_id ?? "", case_type_id: rule.case_type_id ?? "",
      region_id: rule.region_id, regulation: rule.regulation, description: rule.description ?? "",
    });
    setMsg(null);
  }

  async function handleEdit(e: React.FormEvent) {
    e.preventDefault();
    if (!editingId) return;
    let regionId: string | null | undefined = editForm.region_id || null;
    if (regionId?.startsWith(WK_PREFIX)) {
      try { regionId = await resolveRegionId(regionId); }
      catch (e: any) { setMsg(e.message); return; }
    }
    const r = await authFetch(`${API}/sovereignty-rules/${editingId}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: editForm.tenant_id || null, case_type_id: editForm.case_type_id || null,
        region_id: regionId || null, regulation: editForm.regulation || null,
        description: editForm.description || null,
      }),
    });
    if (r.ok) { setEditingId(null); setMsg(null); await load(); }
    else { const err = await r.json(); setMsg(err.detail ?? "Update failed"); }
  }

  async function confirmAndDelete() {
    if (!confirmDelete) return;
    await authFetch(`${API}/sovereignty-rules/${confirmDelete.id}`, { method: "DELETE" });
    setConfirmDelete(null);
    await load();
  }

  async function handleResolve(e: React.FormEvent) {
    e.preventDefault();
    const r = await authFetch(`${API}/sovereignty-rules/resolve`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant_id: resolveForm.tenant_id || null, case_type_id: resolveForm.case_type_id || null }),
    });
    if (r.ok) setResolveResult(await r.json());
  }

  return (
    <div style={S.body}>
      {confirmDelete && (
        <ConfirmModal
          message={`Delete this sovereignty rule? (${confirmDelete.regulation} — ${confirmDelete.tenant_id ? tenantLabel(confirmDelete.tenant_id) : "all tenants"})`}
          onConfirm={confirmAndDelete}
          onCancel={() => setConfirmDelete(null)}
        />
      )}

      <h2 style={{ margin: "0 0 16px", fontSize: 15, fontWeight: 700 }}>Sovereignty Rules</h2>

      {/* Add Rule form */}
      <div style={{ ...S.card, marginBottom: 20 }}>
        <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 10, color: "var(--text-secondary)" }}>ADD RULE</div>
        {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
        <form onSubmit={handleCreate}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10, marginBottom: 10 }}>
            <div>
              <label style={S.label}>Tenant <span style={{ fontWeight: 400, color: "var(--text-secondary)" }}>(blank = all)</span></label>
              <select style={S.select} value={form.tenant_id} onChange={e => setForm(f => ({ ...f, tenant_id: e.target.value }))}>
                <option value="">— All tenants —</option>
                {tenants.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            </div>
            <div>
              <label style={S.label}>Case Type <span style={{ fontWeight: 400, color: "var(--text-secondary)" }}>(blank = all)</span></label>
              <select style={S.select} value={form.case_type_id} onChange={e => setForm(f => ({ ...f, case_type_id: e.target.value }))}>
                <option value="">— All case types —</option>
                {caseTypes.map(ct => <option key={ct.id} value={ct.id} title={ct.id}>{truncate(ct.name)}</option>)}
              </select>
            </div>
            <div>
              <label style={S.label}>Region</label>
              <RegionSelect value={form.region_id} onChange={v => setForm(f => ({ ...f, region_id: v }))} regions={regions} />
            </div>
            <div>
              <label style={S.label}>Regulation</label>
              <select style={S.select} value={form.regulation} onChange={e => setForm(f => ({ ...f, regulation: e.target.value }))}>
                {REGULATIONS.map(r => <option key={r}>{r}</option>)}
              </select>
            </div>
            <div style={{ gridColumn: "span 2" }}>
              <label style={S.label}>Description (optional)</label>
              <input style={S.input} value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))} placeholder="EU customer data must stay in Frankfurt" />
            </div>
          </div>
          <button type="submit" style={{ ...S.btn, ...S.btnP }}>+ Add Rule</button>
        </form>
      </div>

      {/* Resolve tool */}
      <div style={{ ...S.card, marginBottom: 20 }}>
        <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 10, color: "var(--text-secondary)" }}>RESOLVE REGION</div>
        <form onSubmit={handleResolve} style={{ display: "flex", gap: 10, alignItems: "flex-end" }}>
          <div style={{ flex: 1 }}>
            <label style={S.label}>Tenant</label>
            <select style={S.select} value={resolveForm.tenant_id} onChange={e => setResolveForm(f => ({ ...f, tenant_id: e.target.value }))}>
              <option value="">— Any tenant —</option>
              {tenants.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          </div>
          <div style={{ flex: 1 }}>
            <label style={S.label}>Case Type</label>
            <select style={S.select} value={resolveForm.case_type_id} onChange={e => setResolveForm(f => ({ ...f, case_type_id: e.target.value }))}>
              <option value="">— Any case type —</option>
              {caseTypes.map(ct => <option key={ct.id} value={ct.id} title={ct.id}>{truncate(ct.name)}</option>)}
            </select>
          </div>
          <button type="submit" style={{ ...S.btn, ...S.btnP }}>Resolve →</button>
        </form>
        {resolveResult && (
          <div style={{ marginTop: 10, fontSize: 12 }}>
            {resolveResult.region
              ? <span style={{ color: "#22c55e" }}>✓ Data must go to <b>{resolveResult.region.name}</b> ({resolveResult.region.provider})</span>
              : <span style={{ color: "var(--text-secondary)" }}>No sovereignty rule — any region applies</span>}
          </div>
        )}
      </div>

      {/* Rules table */}
      {rules.length === 0
        ? <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>No rules. All tenants can use any region.</div>
        : (
          <table style={S.tbl}>
            <thead>
              <tr>{["Tenant", "Case Type", "Region", "Regulation", "Description", "Actions"].map(h => <th key={h} style={S.th}>{h}</th>)}</tr>
            </thead>
            <tbody>
              {rules.map(rule => (
                <React.Fragment key={rule.id}>
                  <tr style={{ background: editingId === rule.id ? "var(--accent-light, #ede9fe)" : "transparent" }}>
                    <td style={S.td}>
                      {rule.tenant_id
                        ? <span title={rule.tenant_id}>{tenantLabel(rule.tenant_id)}</span>
                        : <span style={{ color: "var(--text-secondary)", fontStyle: "italic" }}>all tenants</span>}
                    </td>
                    <td style={S.td}>
                      {rule.case_type_id
                        ? <span title={rule.case_type_id}>{truncate(ctLabel(rule.case_type_id) ?? "", 32)}</span>
                        : <span style={{ color: "var(--text-secondary)", fontStyle: "italic" }}>all types</span>}
                    </td>
                    <td style={S.td}><code style={{ fontSize: 11 }}>{regionName(rule.region_id)}</code></td>
                    <td style={S.td}><Badge label={rule.regulation} color="#0d9488" /></td>
                    <td style={{ ...S.td, color: "var(--text-secondary)", fontSize: 11 }}>{rule.description ?? "—"}</td>
                    <td style={{ ...S.td, whiteSpace: "nowrap" as const }}>
                      <button style={{ ...S.btn, ...S.btnS, marginRight: 6 }}
                        onClick={() => editingId === rule.id ? setEditingId(null) : startEdit(rule)}>
                        {editingId === rule.id ? "Cancel" : "Edit"}
                      </button>
                      <button style={{ ...S.btn, ...S.btnD }} onClick={() => setConfirmDelete(rule)}>Delete</button>
                    </td>
                  </tr>
                  {editingId === rule.id && (
                    <tr>
                      <td colSpan={6} style={{ padding: "0 0 4px", background: "var(--bg-surface)" }}>
                        <form onSubmit={handleEdit} style={{ padding: "14px 16px", borderTop: "2px solid var(--accent)", borderBottom: "1px solid var(--border)" }}>
                          {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
                          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10, marginBottom: 10 }}>
                            <div>
                              <label style={S.label}>Tenant</label>
                              <select style={S.select} value={editForm.tenant_id} onChange={e => setEditForm(f => ({ ...f, tenant_id: e.target.value }))}>
                                <option value="">— All tenants —</option>
                                {tenants.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
                              </select>
                            </div>
                            <div>
                              <label style={S.label}>Case Type</label>
                              <select style={S.select} value={editForm.case_type_id} onChange={e => setEditForm(f => ({ ...f, case_type_id: e.target.value }))}>
                                <option value="">— All case types —</option>
                                {caseTypes.map(ct => <option key={ct.id} value={ct.id} title={ct.id}>{truncate(ct.name)}</option>)}
                              </select>
                            </div>
                            <div>
                              <label style={S.label}>Region</label>
                              <RegionSelect value={editForm.region_id} onChange={v => setEditForm(f => ({ ...f, region_id: v }))} regions={regions} />
                            </div>
                            <div>
                              <label style={S.label}>Regulation</label>
                              <select style={S.select} value={editForm.regulation} onChange={e => setEditForm(f => ({ ...f, regulation: e.target.value }))}>
                                {REGULATIONS.map(r => <option key={r}>{r}</option>)}
                              </select>
                            </div>
                            <div style={{ gridColumn: "span 2" }}>
                              <label style={S.label}>Description</label>
                              <input style={S.input} value={editForm.description} onChange={e => setEditForm(f => ({ ...f, description: e.target.value }))} placeholder="Optional description" />
                            </div>
                          </div>
                          <div style={{ display: "flex", gap: 8 }}>
                            <button type="submit" style={{ ...S.btn, ...S.btnP }}>Save Changes</button>
                            <button type="button" style={{ ...S.btn, ...S.btnS }} onClick={() => setEditingId(null)}>Cancel</button>
                          </div>
                        </form>
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        )}
    </div>
  );
}


// ── Tenant Assignments tab ────────────────────────────────────────────────────

function AssignmentsTab() {
  const [regions, setRegions]         = useState<Region[]>([]);
  const [tenants, setTenants]         = useState<Tenant[]>([]);
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [form, setForm]               = useState({ tenant_id: "", region_id: "", assignment_type: "primary" });
  const [migrateForm, setMigrateForm] = useState({ tenant_id: "", target_region_id: "" });
  const [migrateMsg, setMigrateMsg]   = useState<{ ok: boolean; text: string } | null>(null);
  const [migrating, setMigrating]     = useState(false);
  const [selectedId, setSelectedId]   = useState<string | null>(null);
  const [editingId, setEditingId]     = useState<string | null>(null);
  const [editType, setEditType]       = useState("primary");
  const [editError, setEditError]     = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Assignment | null>(null);
  const [assignMsg, setAssignMsg]     = useState<string | null>(null);

  const load = useCallback(async () => {
    const [rr, ar, tr] = await Promise.all([
      authFetch(`${API}/regions`),
      authFetch(`${API}/tenant-assignments`),
      authFetch("/api/v1/tenants"),
    ]);
    if (rr.ok) setRegions((await rr.json()).regions);
    if (ar.ok) setAssignments((await ar.json()).assignments);
    if (tr.ok) { const d = await tr.json(); setTenants(Array.isArray(d) ? d : d.tenants ?? []); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const regionName = (id: string) => regions.find(r => r.id === id)?.name ?? id.slice(0, 12);
  const tenantName = (id: string) => tenants.find(t => t.id === id)?.name ?? id;

  // Current primary region for the selected tenant in the migrate form
  const currentPrimary = assignments.find(
    a => a.tenant_id === migrateForm.tenant_id && a.assignment_type === "primary"
  );

  function selectRow(a: Assignment) {
    const newId = selectedId === a.id ? null : a.id;
    setSelectedId(newId);
    setMigrateMsg(null);
    if (newId) {
      setMigrateForm({ tenant_id: a.tenant_id, target_region_id: "" });
    }
  }

  async function handleAssign(e: React.FormEvent) {
    e.preventDefault();
    const r = await authFetch(`${API}/tenant-assignments`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(form),
    });
    if (r.ok) { setForm({ tenant_id: "", region_id: "", assignment_type: "primary" }); setAssignMsg(null); await load(); }
    else { const err = await r.json(); setAssignMsg(err.detail ?? "Failed to create assignment"); }
  }

  async function handleMigrate(e: React.FormEvent) {
    e.preventDefault();
    if (!migrateForm.tenant_id || !migrateForm.target_region_id) return;
    setMigrating(true); setMigrateMsg(null);
    try {
      const r = await authFetch(`${API}/migrate-tenant`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tenant_id: migrateForm.tenant_id, target_region_id: migrateForm.target_region_id }),
      });
      const data = await r.json();
      if (r.ok && data.status === "success") {
        setMigrateMsg({ ok: true, text: `✓ Migrated to ${data.target_region_name}` });
        setSelectedId(null);
        setMigrateForm({ tenant_id: "", target_region_id: "" });
        await load();
      } else {
        setMigrateMsg({ ok: false, text: `✗ ${data.error ?? data.detail ?? "Migration failed"}` });
      }
    } catch {
      setMigrateMsg({ ok: false, text: "✗ Network error during migration" });
    } finally {
      setMigrating(false);
    }
  }

  async function handleEditSave(id: string) {
    setEditError(null);
    const r = await authFetch(`${API}/tenant-assignments/${id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ assignment_type: editType }),
    });
    if (r.ok) { setEditingId(null); setEditError(null); await load(); }
    else {
      const err = await r.json().catch(() => ({}));
      setEditError(err.detail ?? `Error ${r.status} — could not update assignment`);
    }
  }

  async function confirmAndDelete() {
    if (!confirmDelete) return;
    await authFetch(`${API}/tenant-assignments/${confirmDelete.id}`, { method: "DELETE" });
    if (selectedId === confirmDelete.id) { setSelectedId(null); setMigrateForm({ tenant_id: "", target_region_id: "" }); }
    setConfirmDelete(null);
    await load();
  }

  return (
    <div style={S.body}>
      {confirmDelete && (
        <ConfirmModal
          message={`Remove the "${confirmDelete.assignment_type}" assignment for "${tenantName(confirmDelete.tenant_id)}" → "${regionName(confirmDelete.region_id)}"? This cannot be undone.`}
          onConfirm={confirmAndDelete}
          onCancel={() => setConfirmDelete(null)}
        />
      )}

      <h2 style={{ margin: "0 0 4px", fontSize: 15, fontWeight: 700 }}>Tenant Region Assignments</h2>
      <p style={{ margin: "0 0 16px", fontSize: 12, color: "var(--text-secondary)" }}>
        Click any row below to select it and auto-fill the Migrate form.
      </p>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 20 }}>
        {/* Assign form */}
        <div style={S.card}>
          <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 10, color: "var(--text-secondary)" }}>ASSIGN TENANT</div>
          {assignMsg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{assignMsg}</div>}
          <form onSubmit={handleAssign}>
            <div style={{ marginBottom: 8 }}>
              <label style={S.label}>Tenant</label>
              <select style={S.select} value={form.tenant_id} onChange={e => setForm(f => ({ ...f, tenant_id: e.target.value }))} required>
                <option value="">— select tenant —</option>
                {tenants.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            </div>
            <div style={{ marginBottom: 8 }}>
              <label style={S.label}>Region</label>
              <select style={S.select} value={form.region_id} onChange={e => setForm(f => ({ ...f, region_id: e.target.value }))} required>
                <option value="">— select region —</option>
                {regions.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
              </select>
            </div>
            <div style={{ marginBottom: 12 }}>
              <label style={S.label}>Assignment Type</label>
              <select style={S.select} value={form.assignment_type} onChange={e => setForm(f => ({ ...f, assignment_type: e.target.value }))}>
                {ASSIGN_TYPES.map(t => <option key={t}>{t}</option>)}
              </select>
            </div>
            <button type="submit" style={{ ...S.btn, ...S.btnP }}>Assign</button>
          </form>
        </div>

        {/* Migrate form */}
        <div style={{ ...S.card, borderLeft: selectedId ? "3px solid var(--accent)" : "1px solid var(--border)" }}>
          <div style={{ marginBottom: 10 }}>
            <span style={{ fontSize: 12, fontWeight: 700, color: "var(--text-secondary)" }}>MIGRATE TENANT</span>
            <span style={{ fontSize: 11, color: "var(--text-secondary)", fontWeight: 400, marginLeft: 8 }}>· zero-downtime, current primary becomes a replica</span>
          </div>

          {!selectedId && (
            <div style={{ fontSize: 12, color: "var(--text-secondary)", padding: "10px 0", fontStyle: "italic" }}>
              ← Select a row from the table to auto-fill, or choose manually below.
            </div>
          )}

          <form onSubmit={handleMigrate}>
            <div style={{ marginBottom: 8 }}>
              <label style={S.label}>Tenant</label>
              <select style={S.select} value={migrateForm.tenant_id}
                onChange={e => { setMigrateForm(f => ({ ...f, tenant_id: e.target.value, target_region_id: "" })); setSelectedId(null); setMigrateMsg(null); }} required>
                <option value="">— select tenant —</option>
                {tenants.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            </div>

            {/* Show current primary region as contextual info */}
            {migrateForm.tenant_id && (
              <div style={{ fontSize: 11, color: "var(--text-secondary)", marginBottom: 8, padding: "6px 10px", background: "var(--bg-main)", borderRadius: 4, border: "1px solid var(--border)" }}>
                Current primary: {currentPrimary ? <b>{regionName(currentPrimary.region_id)}</b> : <i>none assigned</i>}
              </div>
            )}

            <div style={{ marginBottom: 12 }}>
              <label style={S.label}>Target Region <span style={{ fontWeight: 400, color: "var(--text-secondary)" }}>(new primary)</span></label>
              <select style={S.select} value={migrateForm.target_region_id}
                onChange={e => setMigrateForm(f => ({ ...f, target_region_id: e.target.value }))} required>
                <option value="">— select target region —</option>
                {regions
                  .filter(r => r.id !== currentPrimary?.region_id)
                  .map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
              </select>
            </div>

            <button type="submit" disabled={migrating || !migrateForm.tenant_id || !migrateForm.target_region_id}
              style={{ ...S.btn, ...S.btnP, opacity: migrating ? 0.7 : 1 }}>
              {migrating ? "Migrating…" : "Migrate →"}
            </button>
          </form>

          {migrateMsg && (
            <div style={{ marginTop: 10, fontSize: 12, fontWeight: 600, color: migrateMsg.ok ? "#22c55e" : "#ef4444" }}>
              {migrateMsg.text}
            </div>
          )}
        </div>
      </div>

      {/* Assignments table */}
      {assignments.length === 0
        ? <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>No assignments. Tenants use the global primary region.</div>
        : (
          <table style={S.tbl}>
            <thead>
              <tr>{["", "Tenant", "Region", "Type", "Migrated At", "Actions"].map(h => <th key={h} style={S.th}>{h}</th>)}</tr>
            </thead>
            <tbody>
              {assignments.map(a => {
                const isSelected = selectedId === a.id;
                const isEditing  = editingId === a.id;
                return (
                  <React.Fragment key={a.id}>
                    <tr
                      onClick={() => selectRow(a)}
                      style={{
                        cursor: "pointer",
                        background: isSelected ? "#ede9fe" : isEditing ? "#f0fdf4" : "transparent",
                        outline: isSelected ? "2px solid #0d9488" : "none",
                        outlineOffset: -1,
                      }}
                    >
                      <td style={{ ...S.td, width: 24 as const }}>
                        {isSelected && <span style={{ color: "#0d9488", fontWeight: 700 }}>→</span>}
                      </td>
                      <td style={S.td}>{tenantName(a.tenant_id)}</td>
                      <td style={S.td}><code style={{ fontSize: 11 }}>{regionName(a.region_id)}</code></td>
                      <td style={S.td}><Badge label={a.assignment_type} color={a.assignment_type === "primary" ? "#22c55e" : "#94a3b8"} /></td>
                      <td style={{ ...S.td, fontSize: 11, color: "var(--text-secondary)" }}>{fmtDate(a.migrated_at)}</td>
                      <td style={{ ...S.td, whiteSpace: "nowrap" as const }} onClick={e => e.stopPropagation()}>
                        <button style={{ ...S.btn, ...S.btnS, marginRight: 6 }}
                          onClick={() => { setEditingId(isEditing ? null : a.id); setEditType(a.assignment_type); setEditError(null); }}>
                          {isEditing ? "Cancel" : "Edit"}
                        </button>
                        <button style={{ ...S.btn, ...S.btnD }} onClick={() => setConfirmDelete(a)}>Delete</button>
                      </td>
                    </tr>
                    {isEditing && (
                      <tr>
                        <td colSpan={6} style={{ padding: "0 0 4px", background: "#f0fdf4" }}>
                          <div style={{ padding: "12px 16px", borderTop: "2px solid #22c55e", borderBottom: "1px solid #bbf7d0" }}>
                            {editError && (
                              <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8, padding: "6px 10px", background: "#fef2f2", border: "1px solid #fecaca", borderRadius: 4 }}>
                                {editError}
                              </div>
                            )}
                            <div style={{ display: "flex", alignItems: "flex-end", gap: 10 }}>
                              <div style={{ flex: 1 }}>
                                <label style={S.label}>Assignment Type</label>
                                <select style={S.select} value={editType} onChange={e => { setEditType(e.target.value); setEditError(null); }}>
                                  {ASSIGN_TYPES.map(t => <option key={t}>{t}</option>)}
                                </select>
                              </div>
                              <button style={{ ...S.btn, ...S.btnP }} onClick={() => handleEditSave(a.id)}>Save</button>
                              <button style={{ ...S.btn, ...S.btnS }} onClick={() => { setEditingId(null); setEditError(null); }}>Cancel</button>
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        )}
    </div>
  );
}


// ── Health tab ────────────────────────────────────────────────────────────────

function HealthTab() {
  const [items, setItems]   = useState<HealthItem[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    const r = await authFetch(`${API}/health`);
    if (r.ok) setItems((await r.json()).health);
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div style={S.body}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>Region Health</h2>
        <button style={{ ...S.btn, ...S.btnS }} disabled={loading} onClick={load}>{loading ? "Polling…" : "↻ Poll Now"}</button>
      </div>
      {items.length === 0
        ? <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>No enabled regions. Add a region first.</div>
        : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 12 }}>
            {items.map(item => {
              const color = STATUS_COLOR[item.status] ?? "#94a3b8";
              return (
                <div key={item.region_id} style={{ ...S.card, borderLeft: `3px solid ${color}`, marginBottom: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                    <span style={{ fontSize: 13, fontWeight: 700, flex: 1 }}>{item.region_name}</span>
                    <span style={{ fontSize: 11, color, fontWeight: 700 }}>● {item.status}</span>
                  </div>
                  <div style={{ display: "flex", gap: 16, fontSize: 11 }}>
                    <span>Latency: <b>{item.latency_ms}ms</b></span>
                    {item.replication_lag_ms != null && <span>Lag: <b>{item.replication_lag_ms}ms</b></span>}
                  </div>
                  {item.error_msg && <div style={{ marginTop: 6, fontSize: 10, color: "#ef4444" }}>{item.error_msg}</div>}
                </div>
              );
            })}
          </div>
        )}
    </div>
  );
}


// ── Access Log tab ────────────────────────────────────────────────────────────

type UserDir = { user_id: string; display_name: string };

function AccessLogTab() {
  const [logs, setLogs]       = useState<AccessLog[]>([]);
  const [regions, setRegions] = useState<Region[]>([]);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [users, setUsers]     = useState<UserDir[]>([]);

  const load = useCallback(async () => {
    const [lr, rr, tr, ur] = await Promise.all([
      authFetch(`${API}/access-log`),
      authFetch(`${API}/regions`),
      authFetch("/api/v1/tenants"),
      authFetch("/api/v1/user-directory"),
    ]);
    if (lr.ok) setLogs((await lr.json()).logs);
    if (rr.ok) setRegions((await rr.json()).regions);
    if (tr.ok) { const d = await tr.json(); setTenants(Array.isArray(d) ? d : d.tenants ?? []); }
    if (ur.ok) { const d = await ur.json(); setUsers(Array.isArray(d) ? d : d.users ?? d.items ?? []); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const regionName = (id: string) => regions.find(r => r.id === id)?.name ?? id.slice(0, 8);
  const tenantName = (id: string | null) => id ? (tenants.find(t => t.id === id)?.name ?? id.slice(0, 12)) : "—";
  const actorName  = (id: string | null) => id ? (users.find(u => u.user_id === id)?.display_name ?? id) : "—";

  return (
    <div style={S.body}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>Cross-Region Access Log</h2>
        <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>GDPR Article 30 — immutable record</div>
      </div>
      {logs.length === 0
        ? <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>No access log entries yet.</div>
        : (
          <table style={S.tbl}>
            <thead><tr>{["Region", "Tenant", "Actor", "Action", "Resource", "Legal Basis", "Time"].map(h => <th key={h} style={S.th}>{h}</th>)}</tr></thead>
            <tbody>
              {logs.map(log => (
                <tr key={log.id}>
                  <td style={S.td}><code style={{ fontSize: 10 }}>{regionName(log.region_id)}</code></td>
                  <td style={S.td}>{tenantName(log.tenant_id)}</td>
                  <td style={{ ...S.td, fontSize: 10, color: "var(--text-secondary)" }} title={log.actor_id ?? ""}>{actorName(log.actor_id)}</td>
                  <td style={S.td}><Badge label={log.action} color="#0d9488" /></td>
                  <td style={{ ...S.td, fontSize: 10 }}>{log.resource ?? "—"}</td>
                  <td style={{ ...S.td, fontSize: 10, color: "var(--text-secondary)" }}>{log.legal_basis ?? "—"}</td>
                  <td style={{ ...S.td, fontSize: 10, color: "var(--text-secondary)" }}>{fmtDate(log.recorded_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
    </div>
  );
}


// ── Root ──────────────────────────────────────────────────────────────────────

export default function HxGlobal() {
  const [tab, setTab] = useState<"regions" | "sovereignty" | "assignments" | "health" | "access-log">("regions");
  const tabs = [
    { key: "regions"     as const, label: "Regions" },
    { key: "sovereignty" as const, label: "Sovereignty Rules" },
    { key: "assignments" as const, label: "Tenant Assignments" },
    { key: "health"      as const, label: "Health" },
    { key: "access-log"  as const, label: "Access Log" },
  ];
  return (
    <div style={S.page}>
      <div style={S.tabBar}>
        {tabs.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)}
            style={{ ...S.tab, ...(tab === t.key ? S.tabActive : {}) }}>{t.label}</button>
        ))}
      </div>
      <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
        {tab === "regions"      && <RegionsTab />}
        {tab === "sovereignty"  && <SovereigntyTab />}
        {tab === "assignments"  && <AssignmentsTab />}
        {tab === "health"       && <HealthTab />}
        {tab === "access-log"   && <AccessLogTab />}
      </div>
    </div>
  );
}
