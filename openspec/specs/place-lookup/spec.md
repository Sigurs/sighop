# place-lookup Specification

## Purpose

Names the place an advertised position lies in — neighborhood where known, city or town, and
country — from a gazetteer bundled with sighop, so no coordinates leave the host to be named.

## Requirements
### Requirement: A position is named from a bundled gazetteer
The system SHALL name a position from a gazetteer shipped with sighop, derived from GeoNames
populated places with a population of at least 1000 and GeoNames country names. Naming a position
SHALL NOT make any network request. The gazetteer SHALL be regenerable from GeoNames downloads by a
maintainer script, and the project SHALL credit GeoNames as the data's source under its CC BY 4.0
licence.

#### Scenario: Naming needs no network
- **WHEN** a position is named on a host with no network access
- **THEN** the place is named from the bundled gazetteer

#### Scenario: A position in a known city
- **WHEN** the position 59.329460, 18.068580 is named
- **THEN** the city is Stockholm and the country is Sweden with country code `SE`

### Requirement: A place is the nearest town within reach, its country, and a close neighborhood
The system SHALL name as city the nearest populated place that is not a section of another place,
provided it lies within 50 km of the position, and SHALL name the country that city belongs to by
its English name and ISO 3166-1 alpha-2 code. The system SHALL name as neighborhood the nearest
section of a populated place within 3 km of the position, and SHALL NOT name one when none lies that
close or when its name equals the city's. When no city lies within 50 km the position SHALL have no
place at all — no country is guessed on its own. Distances SHALL be great-circle distances, and
equal distances SHALL be broken the same way on every run.

#### Scenario: A position in a city with a named section nearby
- **WHEN** a position lies 1 km from a city section `Södermalm` of the city `Stockholm`
- **THEN** the place is neighborhood `Södermalm`, city `Stockholm`, country `Sweden`

#### Scenario: No section close by
- **WHEN** the nearest city section lies 5 km from the position
- **THEN** the place has a city and a country and no neighborhood

#### Scenario: Open sea
- **WHEN** no populated place lies within 50 km of the position
- **THEN** the position has no place

#### Scenario: Across the antimeridian
- **WHEN** a position at longitude 179.99 lies 10 km from a town at longitude -179.95
- **THEN** that town is found as the city

### Requirement: The gazetteer never blocks reception and its failure is contained
The system SHALL load the gazetteer at most once per process, off the event loop, and only when a
position is first named. When the gazetteer cannot be loaded the system SHALL log the failure once,
SHALL treat every position as having no place from then on, and SHALL continue otherwise unaffected.

#### Scenario: A damaged data file
- **WHEN** the bundled gazetteer cannot be read
- **THEN** one error is logged and positions are reported with no place, and webhooks are still delivered

#### Scenario: Loaded once
- **WHEN** several positions are named in one run
- **THEN** the gazetteer is read from disk once
