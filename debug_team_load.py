import yaml

with open('config/teams.yaml', 'r') as f:
    config = yaml.safe_load(f)
    print('Raw config:')
    print(config)
    print('\n' + '='*50)
    
    teams = config.get('teams', [])
    print(f'Number of teams: {len(teams)}')
    
    for team in teams:
        print(f"\nTeam: {team.get('name')}")
        print(f"  API Key: {team.get('api_key')}")
        print(f"  Allowed Models: {team.get('allowed_models', [])}")