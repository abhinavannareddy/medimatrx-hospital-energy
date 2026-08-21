// ---------------------------------------------------------------------------
// The hospital's energy zones.
//
// A "zone" is a physical part of the hospital that has its own electricity
// meter. Real hospitals are sub-metered exactly like this.
//
//   critical  = true  -> life-safety load. NEVER shift or reduce this.
//   shiftable = true  -> the work still has to happen today, but it does not
//                        matter WHEN. These are the loads we can move to
//                        cheap-electricity hours to save money.
//   baselineKw        = typical average power draw of that zone, in kilowatts.
// ---------------------------------------------------------------------------

const ZONES = [
  {
    zoneId: 'icu',
    name: 'Intensive Care Unit',
    critical: true,
    shiftable: false,
    baselineKw: 145,
    description: 'Ventilators, monitors, infusion pumps, isolation-room airflow.'
  },
  {
    zoneId: 'theatres',
    name: 'Operating Theatres',
    critical: true,
    shiftable: false,
    baselineKw: 210,
    description: 'Surgical lighting, anaesthesia machines, laminar-flow ventilation.'
  },
  {
    zoneId: 'imaging',
    name: 'MRI & Diagnostic Imaging',
    critical: true,
    shiftable: false,
    baselineKw: 190,
    description: 'MRI cryo-cooling runs 24/7, CT and X-ray follow the clinic list.'
  },
  {
    zoneId: 'wards',
    name: 'Inpatient Wards',
    critical: true,
    shiftable: false,
    baselineKw: 165,
    description: 'Patient rooms, nurse stations, bed lifts, corridor lighting.'
  },
  {
    zoneId: 'hvac',
    name: 'HVAC Central Plant',
    critical: false,
    shiftable: true,
    baselineKw: 320,
    description: 'Chillers and air-handling units. Thermal mass lets us pre-cool.'
  },
  {
    zoneId: 'sterilisation',
    name: 'Sterilisation (CSSD)',
    critical: false,
    shiftable: true,
    baselineKw: 118,
    description: 'Autoclaves and washer-disinfectors. Batch work, deadline-driven.'
  },
  {
    zoneId: 'laundry',
    name: 'Laundry & Linen',
    critical: false,
    shiftable: true,
    baselineKw: 96,
    description: 'Industrial washers and dryers. Must finish before morning rounds.'
  },
  {
    zoneId: 'catering',
    name: 'Catering & Kitchen',
    critical: false,
    shiftable: true,
    baselineKw: 74,
    description: 'Cold storage is constant, but bulk cooking and dishwashing can move.'
  },
  {
    zoneId: 'admin',
    name: 'Administration Block',
    critical: false,
    shiftable: false,
    baselineKw: 58,
    description: 'Offices, IT room, meeting rooms. Office-hours load profile.'
  }
];

// A rough 24-hour shape for each zone, as a multiplier on baselineKw.
// index 0 = 00:00, index 23 = 23:00.
const PROFILES = {
  // Critical care is flat around the clock - that is the whole point of it.
  flat:    [1.00,0.98,0.97,0.97,0.98,1.00,1.03,1.06,1.08,1.09,1.09,1.08,1.07,1.07,1.08,1.08,1.07,1.05,1.03,1.02,1.01,1.01,1.00,1.00],
  // Theatres and imaging follow the daytime clinic list.
  daytime: [0.35,0.32,0.30,0.30,0.33,0.45,0.70,0.95,1.25,1.45,1.50,1.48,1.35,1.42,1.48,1.44,1.25,0.95,0.70,0.58,0.50,0.45,0.40,0.37],
  // HVAC tracks outside temperature and occupancy.
  hvacish: [0.55,0.50,0.48,0.47,0.50,0.62,0.85,1.10,1.30,1.45,1.55,1.62,1.68,1.70,1.65,1.55,1.38,1.15,0.95,0.82,0.72,0.66,0.60,0.57],
  // Batch plant: today it all runs in the day shift, which is the problem.
  batch:   [0.15,0.12,0.10,0.10,0.12,0.30,0.75,1.30,1.65,1.70,1.60,1.55,1.40,1.60,1.68,1.55,1.20,0.80,0.45,0.30,0.22,0.18,0.16,0.15],
  // Offices.
  office:  [0.12,0.10,0.10,0.10,0.11,0.15,0.35,0.75,1.30,1.55,1.60,1.55,1.35,1.50,1.55,1.45,1.10,0.60,0.30,0.20,0.16,0.14,0.13,0.12]
};

const ZONE_PROFILE = {
  icu: 'flat',
  theatres: 'daytime',
  imaging: 'flat',
  wards: 'flat',
  hvac: 'hvacish',
  sterilisation: 'batch',
  laundry: 'batch',
  catering: 'batch',
  admin: 'office'
};

module.exports = { ZONES, PROFILES, ZONE_PROFILE };
