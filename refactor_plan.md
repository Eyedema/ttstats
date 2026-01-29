# 2v2 Match Support - Code Review & Refactor Plan

**Branch:** `support_doubles`
**Date:** 2026-01-29
**Reviewer:** Senior Django Lead

---

## Summary of Changes Detected

The codebase has been migrated from a 1v1-only table tennis tracking system to support both 1v1 and 2v2 (doubles) matches. Key architectural changes:

1. **New Team Model:** Introduced `Team` model with ManyToMany relationship to `Player`
2. **Match Model Refactor:**
   - Replaced `player1`/`player2` ForeignKeys with `team1`/`team2` ForeignKeys
   - Added `is_double` boolean flag
   - Changed `winner` from Player FK to Team FK
   - Replaced boolean confirmation fields with ManyToMany `confirmations` through `MatchConfirmation` model
3. **Game Model Updates:** Renamed `player1_score`/`player2_score` to `team1_score`/`team2_score`, changed `winner` from Player to Team
4. **ScheduledMatch Updates:** Migrated from `player1`/`player2` to `team1`/`team2`
5. **Migrations:** 4 new migrations (0008-0013) handle data migration from old structure to new

**Migration Strategy:** The migrations use data migration functions to create Team objects from existing player relationships and preserve historical data.

**UI/UX Strategy (IMPORTANT):** The future UI will allow users to:
1. Toggle between singles (1v1) and doubles (2v2) matches
2. Select individual players to form teams (not pre-existing teams)
3. For singles: Select 2 players
4. For doubles: Select 4 players (2 per team)
5. Teams will be created programmatically from player selections

**Form Architecture:** To support this UX:
- Forms will have `player1`, `player2`, `player3`, `player4` fields for player selection
- Forms will NOT expose `team1`/`team2` directly (these are created programmatically)
- The view's `form_valid()` or a custom `save()` method will create Team objects from selected players
- Team creation logic: For singles, create single-player teams; for doubles, create two-player teams

---

## Critical Issues (Will Break the App)

### 1. **forms.py - Completely Broken Form Structure** ⚠️ BLOCKING

**Location:** `ttstats/pingpong/forms.py`

**Issues:**
- Line 11: `MatchForm.Meta.fields` references `['is_double', 'team1', 'team2', ...]` but the widgets section (lines 13-52) still defines widgets for non-existent `player1`, `player2`, `player3`, `player4` fields
- Lines 24-35: Widget definitions for `player1`, `player2`, `player3`, `player4` will cause KeyError
- Lines 54-75: `MatchForm.clean()` method validates `player1`, `player2`, `player3`, `player4` which don't exist in cleaned_data
- Lines 176-182: `ScheduledMatchForm.clean()` validates `player1`/`player2` that don't exist
- Lines 157-162: `ScheduledMatchForm` widgets reference `player1`/`player2` fields

**Why It Breaks:** Django will raise `KeyError` or `FieldError` when trying to access/validate non-existent form fields. Users cannot create or edit matches.

**Action Required:**
- Add player1, player2, player3, player4 as ModelChoiceField (queryset=Player.objects.all())
- Make player3, player4 optional (required=False) for singles matches
- Remove team1, team2 from Meta.fields (will be created in view)
- Fix is_double widget (should be Select, not ChoiceField)
- Update all widget definitions to reference player1-4 (fix the existing ones)
- Rewrite `MatchForm.clean()` to:
  - Validate all 4 players are unique (if doubles)
  - Validate player1 != player2 (if singles)
  - Validate player3 and player4 are None/blank for singles matches
- For ScheduledMatchForm: Same approach (player1/player2 fields, create teams in view)

---

### 2. **views.py - MatchCreateView Still Uses Old Field Names** ⚠️ BLOCKING

**Location:** `ttstats/pingpong/views.py:291-417`

**Issues:**
- Line 366: `form.is_double = (form.cleaned_data.get('is_double') == 'True')` - Sets attribute on form instead of instance
- Lines 367-370: Accesses `player1`, `player2`, `player3`, `player4` from cleaned_data which don't exist anymore
- Lines 372-374: References undefined `form.player3` and `form.player4`
- Line 384: Uses bitwise OR `|` instead of logical `or` operator
- Lines 396-407: Validation logic assumes player1/player2 fields exist
- Lines 326-348: `get_form()` tries to access and disable `player1`, `player2`, `player3`, `player4` fields

**Why It Breaks:** `KeyError` when accessing `cleaned_data['player1']`. Form customization fails. Non-staff users cannot create matches.

**Action Required:**
- Refactor `form_valid()` to work with team1/team2 instead of individual players
- For non-staff users, create teams dynamically from the selected players
- Update validation to ensure teams don't have overlapping players
- Fix bitwise OR to logical OR on line 384
- Refactor `get_form()` to work with new team-based structure
- Consider UI/UX: How do non-staff users select team members? May need custom widget or multi-step form

---

### 3. **views.py - ScheduledMatchCreateView Uses Old Fields** ⚠️ BLOCKING

**Location:** `ttstats/pingpong/views.py:894-993`

**Issues:**
- Lines 922-930: `get_form()` references `player1` and `player2` fields that don't exist
- Lines 945-962: `form_valid()` accesses `player1` and `player2` from cleaned_data
- Lines 977-978: Sends emails using player1/player2 variables that won't exist

**Why It Breaks:** `KeyError` when accessing non-existent fields. Scheduled matches cannot be created.

**Action Required:**
- Update `get_form()` to work with team1/team2 fields
- Update `form_valid()` to extract players from teams or work with teams directly
- Update email sending logic to iterate over team.players.all()

---

### 4. **managers.py - MatchManager Uses Deleted Fields** ⚠️ BLOCKING

**Location:** `ttstats/pingpong/managers.py:5-38`

**Issues:**
- Lines 33-34: `visible_to()` filters by `Q(player1=user_player) | Q(player2=user_player)` but these fields were removed in migration 0010

**Why It Breaks:** `FieldError: Cannot resolve keyword 'player1' into field`. All match queries for non-staff users will fail.

**Action Required:**
- Update filter to use: `Q(team1__players=user_player) | Q(team2__players=user_player)`

---

### 5. **models.py - Typo in Related Name** 🐛 BUG

**Location:** `ttstats/pingpong/models.py:349`

**Issue:**
- `ScheduledMatch.team2` has `related_name="scheduled_matches_as_team22"` (double "2")
- Should be `scheduled_matches_as_team2`

**Why It Breaks:** Doesn't break immediately, but creates inconsistent related_name pattern and could cause confusion when accessing reverse relationships.

**Action Required:**
- Fix related_name to `scheduled_matches_as_team2`
- Requires a migration to change the related_name (though related_name is metadata, might not require data migration)

---

## Logical/Architecture Warnings

### 6. **views.py - MatchDetailView Incorrect Player Check** ⚠️ LOGIC ERROR

**Location:** `ttstats/pingpong/views.py:62-76`

**Issue:**
- Line 73: `elif change.player == match.team2.players.all():` compares a single Player to a QuerySet
- Should use `in` operator: `elif change.player in match.team2.players.all():`

**Impact:** Elo changes for team2 players will never display in the template context because the condition always fails.

**Action Required:**
- Change line 73 to use `in` operator for proper membership check

---

### 7. **views.py - PlayerDetailView Winner Filter** ⚠️ LOGIC ERROR

**Location:** `ttstats/pingpong/views.py:79-158`

**Issue:**
- Line 131: `player_won = match.winner.filter(players=self.object).exists()`
- `match.winner` is now a ForeignKey to Team, not a QuerySet, so `.filter()` will raise AttributeError

**Impact:** Streak calculation will crash when trying to determine if player won a match.

**Action Required:**
- Change to: `player_won = self.object in match.winner.players.all()` or `match.winner.players.filter(pk=self.object.pk).exists()`

---

### 8. **views.py - LeaderboardView Incorrect Data Structure** ⚠️ LOGIC ERROR

**Location:** `ttstats/pingpong/views.py:448-500`

**Issue:**
- Line 456: `player_stats` is initialized as a QuerySet from `Player.objects.annotate(...)`
- Lines 486-492: The code tries to append dictionaries to `player_stats` which is a QuerySet, not a list
- This will raise `AttributeError: 'QuerySet' object has no attribute 'append'`

**Impact:** Leaderboard page will crash with 500 error.

**Action Required:**
- The logic seems confused - the annotations already add the stats to each player object
- The append section should be removed OR player_stats should be converted to a list first
- Clarify intended behavior: Are we sorting Player objects or dictionaries?

---

### 9. **views.py - HeadToHeadStatsView Unsafe .first() Calls** ⚠️ POTENTIAL RUNTIME ERROR

**Location:** `ttstats/pingpong/views.py:503-743`

**Issues:**
- Lines 570, 647, 649, 656, 660: Multiple `.first()` calls on `team.players` without null checks
- If a team has no players (orphaned Team object), `.first()` returns None and comparisons will fail

**Impact:** Could cause NoneType comparison errors if data integrity is compromised.

**Action Required:**
- Add null checks or use `.get()` with try/except
- Consider data integrity constraints: Can a Team exist without players? Should we add a constraint?

---

### 10. **emails.py - Potential Attribute Access Issue** ⚠️ MINOR

**Location:** `ttstats/pingpong/emails.py:156`

**Issue:**
- Line 156: `opponent_name = f"{scheduled_match.team2.name}"`
- If team2.name is blank, Team.__str__() generates name from players, but here we directly access `.name` which might be empty string

**Impact:** Email might show empty opponent name instead of generated "Player1 and Player2" format.

**Action Required:**
- Change to `opponent_name = str(scheduled_match.team2)` to use the __str__ method

---

### 11. **forms.py - Widget Type Error** ⚠️ BLOCKING

**Location:** `ttstats/pingpong/forms.py:13-19`

**Issue:**
- Lines 13-19: `is_double` widget is assigned to `forms.ChoiceField(...)` but widgets expect a widget instance, not a field
- Should be: `'is_double': forms.Select(choices=[...], attrs={...})`

**Why It Breaks:** TypeError when rendering form - widgets dict expects Widget instances, not Field instances.

**Action Required:**
- Replace ChoiceField with Select widget
- Move choices to the field definition in Meta or as a separate field override

---

### 12. **views.py - GameCreateView Winner Display Logic** ⚠️ UX ISSUE

**Location:** `ttstats/pingpong/views.py:273`

**Issue:**
- Line 273: Comment indicates uncertainty about verb conjugation for teams
- `f"{self.match.winner} wins"` - grammatically incorrect if winner is a team like "Alice and Bob wins" should be "win"

**Impact:** Poor UX, grammatically incorrect success messages.

**Action Required:**
- Implement smart conjugation: Check if `match.is_double`, use "win" for teams, "wins" for individuals
- Or use neutral phrasing: "Victory for {self.match.winner}!" or "{self.match.winner} won!"

---

### 13. **Migration Safety - Missing Related Name Fix** ⚠️ CONSISTENCY

**Location:** `ttstats/pingpong/models.py:289`

**Issue:**
- Line 289: Comment `# TODO: before it was won_games, search and replace it!`
- The related_name was changed from `won_games` to `games_won` but the comment suggests this might not be reflected everywhere

**Impact:** If templates or code still reference `team.won_games`, they'll raise AttributeError.

**Action Required:**
- Search entire codebase for `won_games` references
- Update any remaining references to `games_won`
- Remove TODO comment after verification

---

## Proposed Action Plan

### Phase 1: Fix Blocking Form Issues (Priority: CRITICAL)
**Files:** `forms.py`

1. **MatchForm Widget Cleanup**
   - Remove all player1-4 widget definitions
   - Add team1, team2 Select widgets with proper CSS classes
   - Update is_double widget from ChoiceField to Select widget

2. **MatchForm Validation Rewrite**
   - Rewrite clean() method to validate team1/team2 exist and are different
   - For doubles matches, add validation that each team has exactly 2 unique players
   - For singles matches, validate each team has exactly 1 player
   - Ensure all 4 players (in doubles) or 2 players (in singles) are unique

3. **ScheduledMatchForm Updates**
   - Remove player1/player2 widgets
   - Add team1/team2 widgets
   - Update clean() to validate team1/team2 instead of player1/player2

### Phase 2: Fix Blocking View Issues (Priority: CRITICAL)
**Files:** `views.py`

4. **MatchCreateView.get_form() Refactor**
   - Remove all references to player1-4 fields
   - For non-staff users: Decide on UX approach for team creation
     - Option A: Hidden team creation (create teams on-the-fly in form_valid)
     - Option B: Add team selection/creation to form
   - If keeping player-level selection, add logic to create teams from selected players in form_valid()

5. **MatchCreateView.form_valid() Refactor**
   - Remove player1-4 variable extraction
   - Extract team1/team2 from cleaned_data
   - If using player-based UI, create Team objects here and assign to match
   - Fix bitwise OR to logical OR on line 384
   - Update validation logic to work with teams
   - For non-staff: validate user is in one of the teams

6. **ScheduledMatchCreateView Refactor**
   - Update get_form() to work with team1/team2
   - Update form_valid() to extract teams or players from teams
   - Update email sending to iterate over team members: `for player in scheduled_match.team1.players.all(): send_email(...)`

7. **MatchDetailView Fix**
   - Line 73: Change `==` to `in` for player membership check

8. **PlayerDetailView Fix**
   - Line 131: Replace `match.winner.filter(...)` with `self.object in match.winner.players.all()`

9. **LeaderboardView Fix**
   - Clarify intended data structure (QuerySet vs list of dicts)
   - Either remove append logic or convert QuerySet to list first
   - Fix sorting logic accordingly

10. **HeadToHeadStatsView Safety**
    - Add null checks for `.first()` calls
    - Consider using `.get()` with try/except or checking `.exists()` first

11. **GameCreateView UX**
    - Implement smart verb conjugation for winner message
    - Check `match.is_double` and use appropriate grammar

### Phase 3: Fix Blocking Manager Issues (Priority: CRITICAL)
**Files:** `managers.py`

12. **MatchManager.visible_to() Fix**
    - Line 33-34: Replace `Q(player1=user_player) | Q(player2=user_player)` with `Q(team1__players=user_player) | Q(team2__players=user_player)`

### Phase 4: Fix Model Issues (Priority: HIGH)
**Files:** `models.py`, new migration file

13. **ScheduledMatch Related Name Typo**
    - Change `related_name="scheduled_matches_as_team22"` to `"scheduled_matches_as_team2"`
    - Create migration: `python manage.py makemigrations pingpong`
    - Test migration on dev database

14. **Remove TODO Comment**
    - Search codebase for `won_games` references
    - Update any found references to `games_won`
    - Remove TODO comment at line 289

### Phase 5: Fix Email Logic (Priority: MEDIUM)
**Files:** `emails.py`

15. **scheduled_match_email Opponent Name**
    - Line 156: Change `scheduled_match.team2.name` to `str(scheduled_match.team2)`
    - This ensures the Team.__str__() method is used for proper display

### Phase 6: Testing & Verification (Priority: CRITICAL)
**Files:** Various

16. **Manual Testing Checklist**
    - Staff user creates 1v1 match → success
    - Staff user creates 2v2 match → success
    - Non-staff user creates 1v1 match → success
    - Non-staff user creates 2v2 match → success
    - View match detail page → Elo changes display correctly
    - View player detail page → Streak calculation works
    - View leaderboard → No crashes, stats display correctly
    - View head-to-head for two players → Stats calculate correctly
    - Schedule a match → Emails sent correctly
    - Confirm a match → Confirmation system works for all team members

17. **Data Integrity Checks**
    - Verify all existing matches have team1 and team2 assigned
    - Verify no orphaned Team objects (teams with no players)
    - Verify all Game objects have team1_score and team2_score
    - Run: `python manage.py check` to verify no system check errors

### Phase 7: Future Considerations
**Files:** N/A (planning)

18. **UX/UI Decisions Needed**
    - How do users create/select teams for doubles matches?
    - Should teams be persistent (reusable) or ephemeral (created per-match)?
    - Do we need a Team management UI (list, create, edit teams)?
    - Should team names be auto-generated or user-editable?

19. **ELO System for Doubles**
    - Current elo.py explicitly skips doubles matches (line 87-89)
    - Need to design Elo calculation for 2v2 (average team rating? individual adjustments?)
    - Implement calculate_elo_2v2() function

20. **Test Coverage**
    - After fixing logic errors, rebuild test suite
    - Ensure coverage for both 1v1 and 2v2 paths
    - Test team creation/validation thoroughly

---

## Execution Order

**CRITICAL PATH (Must fix before app is functional):**
1. Fix forms.py (Phase 1) - Otherwise no matches can be created
2. Fix managers.py (Phase 3) - Otherwise non-staff users get errors
3. Fix views.py MatchCreateView (Phase 2, items 4-5) - Otherwise match creation fails
4. Fix views.py ScheduledMatchCreateView (Phase 2, item 6) - Otherwise scheduling fails
5. Fix views.py other views (Phase 2, items 7-10) - Otherwise viewing matches/stats fails

**HIGH PRIORITY (Fixes bugs but app might partially work):**
6. Fix model typo (Phase 4, item 13)
7. Fix email logic (Phase 5)

**MEDIUM PRIORITY (UX improvements):**
8. Fix winner message grammar (Phase 2, item 11)
9. Remove TODO comment (Phase 4, item 14)

**POST-FIX (After basic functionality restored):**
10. Manual testing (Phase 6)
11. Future planning (Phase 7)

---

## Estimated Complexity

- **Forms fixes:** Medium complexity - Requires understanding team creation flow
- **View fixes:** High complexity - Multiple views need refactoring, UX decisions needed
- **Manager fix:** Low complexity - Simple query change
- **Model fix:** Low complexity - Just a migration
- **Testing:** High complexity - Need to test all permutations of 1v1/2v2, staff/non-staff

---

## Notes for Implementation

1. **Backwards Compatibility:** The migrations preserve historical 1v1 data by creating single-player teams. Ensure this logic is respected in all new code.

2. **Team Lifecycle:** Decide if teams are ephemeral (created/destroyed with matches) or persistent (reusable entities). Current model supports persistent teams but UI doesn't expose team management.

3. **Form UI Strategy:** Consider creating two separate forms (MatchForm1v1 and MatchForm2v2) or using dynamic form fields based on is_double. Current approach tries to handle both in one form which may be complex.

4. **Data Validation:** Add model-level constraints to ensure:
   - Teams in singles matches have exactly 1 player
   - Teams in doubles matches have exactly 2 players
   - All players in a match are unique

5. **Signal Behavior:** Verify signals.py handles both 1v1 and 2v2 correctly - the current implementation iterates over team.players.all() which should work for both cases.

---

## Risk Assessment

**HIGH RISK:**
- Forms and views are currently broken and will cause runtime errors
- Manager filter will prevent non-staff users from accessing any matches
- Without fixes, the app is non-functional for core workflows

**MEDIUM RISK:**
- Logic errors in leaderboard and player detail could cause data display issues
- Email formatting issues are minor but affect user experience

**LOW RISK:**
- Model typo is cosmetic but should be fixed for consistency
- UX issues like grammar are polish items

**RECOMMENDATION:** Prioritize Phase 1-3 (critical path) immediately. The app is currently in a broken state for most users. Phase 4-5 can follow shortly after. Phase 6-7 should be done before merging to master.
