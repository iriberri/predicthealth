import pandas as pd
import numpy as np 
import re, datetime
from dateutil import parser
from os.path import join, dirname
from dotenv import load_dotenv

from twython import Twython, TwythonError
from instagram.client import InstagramAPI

import util 
from collect import collect_instagram, collect_twitter

dotenv_path = join(dirname(__file__), '.env')
load_dotenv(dotenv_path)

data_head = os.environ.get("DATA_PATH")


def collect(conn, test=False, max_collect=10):
	''' Collects social media record of validated study participants.
		- checks for uncollected participant data
		- scrapes, caches, extracts features and writes to db tables
		- note: this should run as a cron job every 15 minutes, but Dreamhost cron is weird. So we usually run manually. '''

	# gets table name / field / datatype for all tables as a Pandas data frame
	table_data = util.get_table_data()

	log_msgs = []
	log_msgs.append('Starting collect\n')

	try:
		query = "SELECT username, user_id, uid, medium FROM usernames WHERE collected=0 AND validated=1 LIMIT {}".format(max_collect)
		cur   = conn.cursor()
		cur.execute(query)
		rows  = cur.fetchall()

		for row in rows:

			username, user_id, unique_id, medium = row
			log_msgs.append('Collect for {} user: {} [ID: {}]'.format(medium, username, user_id))

			CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_SECRET = util.get_tokens(conn, medium, username)

			if medium == "twitter": # big thanks to Andy Reagan here: https://github.com/andyreagan/tweet-stealing
				
				twitter = Twython(CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
				collect_twitter(twitter, username, unique_id, conn, table_data)

			elif medium == "instagram":

				instagram = InstagramAPI(access_token=ACCESS_TOKEN, client_secret=CONSUMER_SECRET)
				
				collect_instagram(instagram, username, unique_id, conn, table_data)
		log_msgs.append('Collect log completed without top-level errors (check individual logs for per-basis errors).')

	except Exception, error:
		log_msgs.append('Collect error: {}'.format(str(error)))

	log_dir = 'collect/batch/'

	for msg in log_msgs:
		print msg
		
	util.log(log_msgs,log_dir)


def get_qualtrics_survey_ids(conn, surveys, table_name='qualtrics_surveys'):
	conditions = []
	survey_set = "('" + "','".join(surveys) + "')"
	query = 'select name, id, condition from {} where name in {}'.format(table_name, survey_set)
	cur = conn.cursor()
	cur.execute(query)
	rows = cur.fetchall()
	for r in rows:
		conditions.append( {"name":r[0], "id":r[1], "condition":r[2]})
	return conditions


def get_qualtrics_survey_data(start_after, start_str, condition, user_id, api_token):
	''' Uses Qualtrics API to pull down raw survey data '''

	# start_after_id is a marker, we start collecting surveys submitted after this entry
	start_after_id = start_after.id[start_after.condition==condition["name"]].values
	start_after_id = start_after_id[0]

	if start_after_id[0]==start_str:
		lastresponse_param = "LastResponseID={lastresponse}&".format(lastresponse=start_after_id)
	else:
		lastresponse_param = ""
		
	qualtrics_url = ("""https://survey.qualtrics.com/WRAPI/ControlPanel/api.php?
					  Request=getLegacyResponseData&
					  User={userid}&
					  Token={token}&
					  Format=CSV&
					  Version=2.5&
					  Labels=1&
					  ExportTags=1&
					  {lastresponse_param}
					  SurveyID={surveyid}"""
					 .format(userid=user_id,
							 token=api_token,
							 lastresponse_param=lastresponse_param,
							 surveyid=condition["id"]
							)
					)
	# formatted api url for readability, but we have to smush it back together before calling
	qualtrics_url = "".join(qualtrics_url.split()) 

	data = pd.read_csv(qualtrics_url)
	# drop first row, which contains verbose column descriptions
	data.drop(0,0,inplace=True)	

	return data


def clean_qualtrics_data(data, condition):
	''' Takes raw Qualtrics survey data, adjusts column names, drops unnecessary fields '''

	# filter qualtrics variables that we don't need (we negate matches on this filter)
	p = re.compile("time_.+[124]|SC0_[12]|SC1_[12]|suspected|active|know_date|share|m_[124]|t_[124]|validated|folw|diagnosis|condition|study_username|handle|medium|been_diag|criteria|agree|location|^cesd_|^tsq|recruitment|follow|v[2345678]|unnamed|consent|cst", flags=re.IGNORECASE)				
	# rename qualtrics columns to database fields
	updated={"unique_id":"uid",
			 "V10":"qualtrics_complete",
			 "V9":"time_finished",
			 "SC0_0":"tsq" if condition["condition"] == "ptsd" else "cesd",
			 "age_check":"year_born",
			 "diag_date#1_1":"diag_month",
			 "diag_date#2_1":"diag_day",
			 "diag_date#3_1":"diag_year",
			 "event_date#1_1":"event_month",
			 "event_date#2_1":"event_day",
			 "event_date#3_1":"event_year",
			 "suspect_ct":"days_suspected",
			 "time_cesd_3":"timer_cesd",
			 "uname_ig":"username_ig",
			 "uname_tw":"username_tw",
			 "\xef\xbb\xbfV1":"response_id",
			 "V1":"response_id"
			}
	# filter out unused qualtrics columns 
	drop_cols = data.filter(regex=p).columns
	data.drop(drop_cols,1,inplace=True)
	# apply "updated" name conversion
	data.columns = [updated[col] if col in updated.keys() else col for col in data.columns]

	# consolidate all possible username fields into two (one for insta, one for twitter)
	#data["username_ig"] = data["username_ig_mturk"]
	try:
		data["share_time_tw"] = data["share_time_3"]
	except:
		pass

	# drop extra username fields
	extra_fields = ["timer_follow_ig_twitter","timer_follow_ig_mturk","timer_follow_tw_twitter","timer_follow_tw_mturk","username_ig_mturk","username_ig_twitter","username_tw_mturk","username_tw_twitter"]
	for field in extra_fields:
		try:
			data.drop(field,1,inplace=True)
		except:
			pass

def write_data_to_study_db(conn, data, condition, start_after):
	''' Writes cleaned survey data to SQLite study-specific databases '''

	fields = tuple(data.columns)
	vals = [tuple(row) for row in data.values]
	# we convert month string into zero-padded integer
	month_dict = {'February': '02', 'October': '10', 'March': '03', 'August': '08', 'May': '05', 'January': '01', 'June': '06', 'September': '09', 'April': '04', 'December': '12', 'July': '07', 'November': '11'}
	data['diag_month'].fillna('',inplace=True)
	data['diag_monthnum'] = [month_dict[mon] if mon != '' else None for mon in data['diag_month']]

	query = "INSERT OR IGNORE INTO {table}{cols} VALUES(".format(table=condition["condition"],cols=fields)
	query += ('?,' *len(fields))[:-1] + ")"				
	try:
		with conn:
			cur = conn.cursor()
			cur.executemany(query, vals)
		conn.commit()			
		# get most recent response_id, then update start_after data
		most_recent = data.response_id.values[-1]
		start_after.ix[start_after.condition==condition["name"],"id"] = most_recent
	except Exception,error:
		start_after.ix[start_after.condition==condition["name"],"id"] = error 

	return start_after 


def update_validated_usernames(conn, data, condition, log_msgs):
	''' Updates `usernames` table with validated status (0/1) of participants, based on total_posts '''

	ct = 0
	switched = []

	for i,row in data.iterrows():
		try:
			medium = row.platform
			if medium == "twitter":
				uname = row.username_tw
			elif medium == "instagram":
				uname = row.username_ig

			query = "SELECT total_posts FROM usernames WHERE username='{}' AND medium='{}'".format(uname,medium)
			with conn:
				cur = conn.cursor()
				cur.execute(query)
				total_posts = cur.fetchone()[0]
			print 'total_posts: {}'.format(total_posts)
			if (int(row.qualtrics_complete)==1) and isinstance(uname,str):
				if total_posts > 5:
					log_msgs.append('VALIDATED:  {cond}  {wid}  {rid}'.format(cond=condition['name'], wid=row.workerId, rid=row.response_id))
					switched.append(uname)
					ct +=1
					query = "UPDATE usernames SET valid_{}='{}', validated=1 WHERE username='{}' AND medium='{}'".format(condition['condition'],row.response_id,uname,medium)
					with conn:
						cur = conn.cursor()
						cur.execute(query)
					conn.commit()
				else:
					log_msgs.append('INVALIDATED:  NOT ENOUGH POSTS: {cond}  {wid}  {rid}'.format(cond=condition['name'], wid=row.workerId, rid=row.response_id))
					query = "UPDATE usernames SET valid_{}='{}', validated=0, collect_error='Not enough posts' WHERE username='{}' AND medium='{}'".format(condition['condition'],row.response_id,uname,medium)
					with conn:
						cur = conn.cursor()
						cur.execute(query)
					conn.commit()
		except:
			pass

	if ct > 0:
		switched_str = "(" + ','.join(switched) + ")"
		log_msgs.append('Switched {} usernames to Validated (ready for collect): [{}]'.format(ct, switched_str))
	else:
		log_msgs.append('No new data to add for {}'.format(condition['name']))


def add_survey_data(conn, test=False, beginning_of_start_after_id_string='R'):
	''' Pulls down survey data via Qualtrics API, cleans, stores to SQLite database '''

	if test:
		surveys = ['test_pregnancy',
				   'test_depression']
	else:
		surveys = ['pregnancy_twitter',
				   'depression_twitter',
				   'cancer_twitter',
				   'ptsd_twitter',
				   'pregnancy_instagram',
				   'depression_instagram',
				   'cancer_instagram',
				   'ptsd_instagram']

	new_data = False
	
	conditions = get_qualtrics_survey_ids(conn, surveys)

	# Qualtrics credentials
	_,_, user_id, api_token = util.get_tokens(conn, "qualtrics")

	# this CSV keeps track of the last survey response ID we've recorded, that's where we start pulling from qualtrics
	start_after_fname = "survey/TEST__most_recent_ids.csv" if test else "survey/most_recent_ids.csv"
	start_after_url = data_head + start_after_fname
	start_after = pd.read_csv(start_after_url)
	
	log_msgs = []

	for condition in conditions:
		
		log_msgs.append('\nStarting add survey data for survey: {}'.format(condition['name']))
		
		# get CSV of survey responses from Qualtrics API call
		data = get_qualtrics_survey_data(start_after, beginning_of_start_after_id_string, condition, user_id, api_token)
		
		if data.shape[0] > 0: # if there are new entries, record to SQL
			new_data = True			
			clean_qualtrics_data(data, condition)
			write_data_to_study_db(conn, data, condition, start_after)
			update_validated_usernames(conn, data, condition, log_msgs)
			
	# write updated start_after data to csv 
	start_after.to_csv(start_after_url, index=False)

	if new_data:
		log_msgs.append("Survey data added successfully.")
	else:
		log_msgs.append("No new data to add.")

	log_dir = 'addsurveydata/'
	if test:
		log_dir = 'test/' + log_dir

	for msg in log_msgs:
		print msg 

	util.log(log_msgs, log_dir)


def add_monthnum(conn):
	''' fixes existing database entries from qualtrics where month is string instead of a zero-padded integer '''

	months = [('January','01'),('February','02'),('March','03'),('April','04'),('May','05'),('June','06'),('July','07'),('August','08'),('September','09'),('October','10'),('November','11'),('December','12')]
	month_dict = {k:v for k,v in months}
	conditions = ['pregnancy','cancer','ptsd','depression']
	for condition in conditions:
		query = 'select uid, diag_month from {} where diag_month is not null and diag_monthnum is null'.format(condition)
		with conn:
			cur = conn.cursor()
			cur.execute(query)
			rows = cur.fetchall()
		params = []
		ct = 0
		for row in rows:
			ct += 1
			uid = row[0] 
			dmonth = row[1] 
			params.append([dmonth, month_dict[dmonth], uid])
		query = "update {} set diag_monthnum = replace(diag_month,?,?) where uid=? and diag_month is not null".format(condition)
		with conn:
			cur = conn.cursor()
			cur.executemany(query, params)
		conn.commit()
		print "Condition: {} | Converted {} months to monthnum".format(condition,ct)


def count_days_from_turning_point(conn):
	''' Counts the number of days from turning point (either diagnosis or suspected date) for each social media
		post, for a given user and a given condition.  
		- Values of counts are +/- integers (-X = X days before turning point, +X = X days after turning point)
		- Count fields are named with the format: d_from_{turning_point}_{condition}, eg. d_from_diag_pregnancy 
		- Attemps count for all rows in meta_ig/meta_tw which lack a count in all conditions 
		  (so might be more rows than the most recent batch of survey respondents) '''

	conditions = ['pregnancy','cancer','ptsd','depression']

	for condition in conditions:
		query = ("SELECT platform, username_tw, username_ig, diag_day, diag_monthnum, diag_year, days_suspected " + 
				 "FROM {} ".format(condition) + 
				 "WHERE ({cond}.username_tw is not null OR {cond}.username_ig is not null) AND ({cond}.diag_year is not null) AND (d_from_{cond}_pregnancy is null AND d_from_{cond}_depression is null AND d_from_{cond}_cancer is null AND d_from_{cond}_ptsd is null)".format(cond=condition)
				 )
		with conn:
			cur = conn.cursor()
			cur.execute(query)
			rows = cur.fetchall()
		print 
		print
		print "CONDITION: {}".format(condition)
		params = []
		for r in rows:
			platform, utw, uig, day, month, year, dsusp = r
			month = '0'+str(month) if len(str(month))==1 else month
			day = '0'+str(day) if len(str(day))==1 else day 
			dates = {}
			ddate = '-'.join([str(year),str(month),str(day)])
			dates['diag'] = ddate
			try:
				ddate_obj = parser.parse(ddate)
			except Exception, e:
				print str(e)
				print 'Problem with date for user: tw: {} ig: {}, date = {}'.format(utw, uig,ddate) 
			if dsusp and str(dsusp)[-1]!='+':
				sdate_obj = ddate_obj - datetime.timedelta(days=dsusp)
				dates['susp'] = sdate_obj.strftime('%Y-%m-%d')
			else:
				dates['susp'] = None
			if utw:
				table_name = 'meta_tw'
				field_name = 'username_tw'
				uname = utw
			elif uig:
				table_name = 'meta_ig'
				field_name = 'username_ig'
				uname = uig
			else:
				table_name = None
			if table_name:
				for date_type in ['diag','susp']:
					if dates[date_type] is not None:
						print
						print "Writing count days for condition: {} | turning point: {}".format(condition,date_type)
						# count number of days difference between post and diag/susp date
						# -int = post date is earlier than diag/susp date, +int=later
						query = "UPDATE {table} SET d_from_{date_type}_{cond}=julianday('{date}')-julianday(created_date) WHERE username='{uname}'".format(table=table_name,cond=condition,date=dates[date_type],date_type=date_type,uname=uname)
						
						with conn:
							cur = conn.cursor()
							cur.execute(query)
						conn.commit()
						print 'Commit complete for {} [TABLE: {}, COND: {}, DTYPE: {}]'.format(uname, table_name, condition, date_type)

if __name__ == '__main__':
	conn = util.connect_db()
	add_survey_data(conn)
	collect(conn)
	add_monthnum(conn)
	count_days_from_turning_point(conn)


	